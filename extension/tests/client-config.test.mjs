import assert from "node:assert/strict";
import test from "node:test";
import {validateClientConfig} from "../client-config.mjs";
import {clientSettings} from "../client-settings.mjs";

const config = () => ({enabled: true, apiBaseUrl: "https://api.example.com/release/",
  cognitoDomain: "https://comment-lens-123456789012.auth.us-east-1.amazoncognito.com/",
  clientId: "fixtureclient1", extensionId: "a".repeat(32),
  resultBaseUrl: "https://fixture-bucket.s3.us-east-1.amazonaws.com/serving/development/"});

test("The checked-in preview is disabled and normalized deployment metadata is immutable", () => {
  assert.deepEqual(clientSettings, {enabled: false});
  assert.ok(Object.isFrozen(clientSettings));
  assert.deepEqual(validateClientConfig(), {enabled: false});
  const result = validateClientConfig(config());
  assert.equal(result.apiBaseUrl, "https://api.example.com/release");
  assert.equal(result.cognitoDomain, "https://comment-lens-123456789012.auth.us-east-1.amazoncognito.com");
  assert.equal(result.resultBaseUrl, "https://fixture-bucket.s3.us-east-1.amazonaws.com/serving/development");
  assert.deepEqual(result.pollScheduleSeconds, [2, 5, 10]);
  assert.equal(result.maxComments, 1000);
  assert.ok(Object.isFrozen(result)); assert.ok(Object.isFrozen(result.pollScheduleSeconds));
});

test("Connection settings reject secrets, partial configurations and overly broad destinations", () => {
  const cases = [null, {}, {enabled: 1}, {enabled: false, apiBaseUrl: "https://api.example.com"},
    {...config(), apiKey: "private-value"}, {...config(), token: "private-value"}];
  for (const field of ["apiBaseUrl", "cognitoDomain", "clientId", "extensionId", "resultBaseUrl"]) {
    const missing = config(); delete missing[field]; cases.push(missing);
  }
  for (const value of ["http://api.example.com", "https://evil.example/?key=private-value", "https://user:private-value@api.example.com",
    "https://api.example.com/#fragment", "https://*.example.com", "https://localhost", "https://127.0.0.1", "https://api.example.com:8080",
    "https://api.example.com/../other", "https://api.example.com/%2e%2e/other", " https://api.example.com", "https://api.example.com\\other"]) {
    cases.push({...config(), apiBaseUrl: value});
  }
  for (const value of ["https://evil.example", "https://x.auth.us-east-1.amazoncognito.com/path", "https://x.auth.us-east-1.amazoncognito.com?x=1"])
    cases.push({...config(), cognitoDomain: value});
  for (const value of ["https://bucket.s3.amazonaws.com", "https://bucket.s3.amazonaws.com/datasets/private",
    "https://bucket.s3.amazonaws.com.evil.example/serving/development", "https://bucket.s3.amazonaws.com/serving/development/../production",
    "https://s3.amazonaws.com/bucket/serving/development"])
    cases.push({...config(), resultBaseUrl: value});
  for (const raw of cases) assert.throws(() => validateClientConfig(raw), error =>
    error.message === "The extension connection settings are invalid" && !error.message.includes("private-value"));
});

test("Polling and comment settings stay within the serving contract", () => {
  assert.equal(validateClientConfig({...config(), maxComments: 25}).maxComments, 25);
  for (const maxComments of [0, 1001, 1.5, "100", true]) assert.throws(() => validateClientConfig({...config(), maxComments}));
  for (const pollScheduleSeconds of [[1, 1, 1], [2, 5], [2, 5, 10, 60], "2,5,10", [2, 5, 10.5]])
    assert.throws(() => validateClientConfig({...config(), pollScheduleSeconds}));
});
