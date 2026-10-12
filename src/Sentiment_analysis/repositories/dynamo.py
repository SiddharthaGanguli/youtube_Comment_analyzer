"""Optimistic transactions on one DynamoDB table.

Domain code reads a version, builds the next value, and commits all affected
items together. A conflicting writer makes the whole transaction fail; it
never leaves half an admission or half a quota release behind.
"""

import json
from dataclasses import dataclass
from uuid import uuid4

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError


@dataclass(frozen=True)
class Item:
    key: str
    version: int
    data: dict


@dataclass(frozen=True)
class Change:
    key: str
    expected: int | None
    data: dict | None
    due_family: str | None = None
    due_at: int | None = None
    cleanup_at: int | None = None
    owner: str | None = None
    created_at: int | None = None
    check_only: bool = False


class DynamoStore:
    DUE_INDEX = "DueIndex"
    CLEANUP_INDEX = "CleanupIndex"
    OWNER_INDEX = "OwnerIndex"

    def __init__(self, client, table_name: str):
        self.client = client
        self.table_name = table_name
        self.serializer = TypeSerializer()
        self.deserializer = TypeDeserializer()

    def _encode(self, data):
        return {key: self.serializer.serialize(value) for key, value in data.items()}

    def _decode(self, raw):
        row = {key: self.deserializer.deserialize(value) for key, value in raw.items()}
        return Item(row["pk"], int(row["version"]), json.loads(row["payload"]))

    def get(self, key: str) -> Item | None:
        response = self.client.get_item(TableName=self.table_name,
                                        Key=self._encode({"pk": key}), ConsistentRead=True)
        return self._decode(response["Item"]) if "Item" in response else None

    def commit(self, changes: list[Change]) -> bool:
        if not changes or len(changes) > 100 or len({c.key for c in changes}) != len(changes):
            raise ValueError("A transaction needs 1..100 distinct item keys")
        actions = []
        for change in changes:
            condition = "attribute_not_exists(pk)" if change.expected is None else "#v = :v"
            action = {"TableName": self.table_name, "ConditionExpression": condition}
            if change.expected is not None:
                action.update(ExpressionAttributeNames={"#v": "version"},
                              ExpressionAttributeValues=self._encode({":v": change.expected}))
            if change.check_only:
                action["Key"] = self._encode({"pk": change.key})
                actions.append({"ConditionCheck": action})
                continue
            if change.data is None:
                action["Key"] = self._encode({"pk": change.key})
                actions.append({"Delete": action})
                continue
            row = {"pk": change.key, "version": (change.expected or 0) + 1,
                   "payload": json.dumps(change.data, sort_keys=True, separators=(",", ":"), allow_nan=False)}
            if len(row["payload"].encode("utf-8")) > 300_000:
                raise ValueError("Metadata is too large; comments belong in S3")
            if change.due_family is not None:
                if change.due_at is None:
                    raise ValueError("An indexed due item needs a timestamp")
                row.update(due_pk=change.due_family, due_at=change.due_at)
            if change.cleanup_at is not None:
                row.update(cleanup_pk="cleanup", cleanup_at=change.cleanup_at)
            if change.owner is not None:
                row.update(owner_pk=change.owner, created_at=change.created_at)
            action["Item"] = self._encode(row)
            actions.append({"Put": action})
        try:
            # Boto3 retries this exact request with the same token after transport errors.
            self.client.transact_write_items(TransactItems=actions, ClientRequestToken=str(uuid4()))
        except ClientError as error:
            if error.response["Error"]["Code"] == "TransactionCanceledException":
                reasons = error.response.get("CancellationReasons", [])
                if any(r.get("Code") in {"ConditionalCheckFailed", "TransactionConflict"} for r in reasons):
                    return False
            raise
        return True

    def due(self, family: str, now: int, *, cleanup=False, limit=100) -> list[Item]:
        index = self.CLEANUP_INDEX if cleanup else self.DUE_INDEX
        partition = "cleanup_pk" if cleanup else "due_pk"
        sort = "cleanup_at" if cleanup else "due_at"
        response = self.client.query(
            TableName=self.table_name, IndexName=index, Limit=limit,
            KeyConditionExpression="#p = :p AND #s <= :s",
            ExpressionAttributeNames={"#p": partition, "#s": sort},
            ExpressionAttributeValues=self._encode({":p": family, ":s": now}),
        )
        # GSI values may be stale. Domain actions re-read and compare the base version.
        return [self._decode(row) for row in response.get("Items", [])]

    def owned(self, owner: str, *, cursor=None, limit=100):
        arguments = dict(TableName=self.table_name, IndexName=self.OWNER_INDEX, Limit=limit,
                         KeyConditionExpression="owner_pk = :owner",
                         ExpressionAttributeValues=self._encode({":owner": owner}))
        if cursor is not None:
            arguments["ExclusiveStartKey"] = cursor
        response = self.client.query(**arguments)
        return ([self._decode(row) for row in response.get("Items", [])], response.get("LastEvaluatedKey"))
