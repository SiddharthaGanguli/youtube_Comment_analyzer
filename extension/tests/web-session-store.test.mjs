import assert from "node:assert/strict";
import test from "node:test";
import {readWebSession, writeWebSession, clearWebSession, WEB_SESSION_SKEW_MS} from "../web-session-store.mjs";

const NOW = 1_792_022_400_000;
const PRIVATE = "private-provider-details";
const config = {enabled: true, apiBaseUrl: "https://fixture.execute-api.us-east-1.amazonaws.com",
  cognitoDomain: "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com", clientId: "fixtureclient1",
  redirectUri: "https://siddharthaganguli.github.io/youtube_Comment_analyzer/dashboard.html",
  resultBaseUrl: "https://fixture-bucket.s3.us-east-1.amazonaws.com/serving/development"};

function jwt(update = {}) {
  const encode = value => Buffer.from(JSON.stringify(value)).toString("base64url");
  return `${encode({alg: "RS256"})}.${encode({token_use: "access", client_id: config.clientId,
    exp: NOW / 1000 + 3600, scope: "openid analysis/read analysis/write", ...update})}.fixture-signature`;
}

function fixture() {
  const data = new Map([["dashboard.preference", "preserve"]]);
  const storage = {getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key)};
  const options = {storage, now: () => NOW};
  const session = {accessToken: jwt(), expiresAtMs: NOW + 1800_000};
  writeWebSession(config, session, options);
  const key = [...data.keys()].find(key => key.startsWith("commentLens.webSession.v1:"));
  return {data, storage, options, session, key};
}

test("A website tab restores the original bounded access session without extending its expiry", () => {
  const f = fixture(), raw = f.data.get(f.key);
  const restored = readWebSession(config, {...f.options, now: () => NOW + 900_000});
  assert.deepEqual(restored, f.session); assert.ok(Object.isFrozen(restored));
  assert.equal(f.data.get(f.key), raw);
  assert.equal(f.key, "commentLens.webSession.v1:" + JSON.stringify([config.cognitoDomain, config.clientId, config.redirectUri]));
  assert.equal(readWebSession(config, {...f.options, now: () => f.session.expiresAtMs - WEB_SESSION_SKEW_MS}), null);
  assert.equal(f.data.has(f.key), false);
  assert.equal(f.data.get("dashboard.preference"), "preserve");
});

test("Session lifetime is capped at one hour and cannot outlive the access token", () => {
  const f = fixture();
  const capped = writeWebSession(config, {accessToken: jwt({exp: NOW / 1000 + 7200}), expiresAtMs: NOW + 7200_000}, f.options);
  assert.equal(capped.expiresAtMs, NOW + 3600_000);
  assert.throws(() => writeWebSession(config, {accessToken: jwt({exp: NOW / 1000 + 600}), expiresAtMs: NOW + 1800_000}, f.options), {code: "AUTH_UNAVAILABLE"});
  assert.equal(f.data.has(f.key), false);
});

test("Malformed, oversized, foreign, expired and unexpected-token records are removed", () => {
  const cases = [
    () => "not-json", () => " ".repeat(20_481), record => ({...record, version: 2}),
    record => ({...record, clientId: "anotherclient"}), record => ({...record, cognitoDomain: "https://other.auth.us-east-1.amazoncognito.com"}),
    record => ({...record, redirectUri: "https://other.example/dashboard.html"}), record => ({...record, storedAtMs: NOW + 1}),
    record => ({...record, storedAtMs: -1}), record => ({...record, expiresAtMs: NOW + 20_000}),
    record => ({...record, expiresAtMs: "later"}), record => ({...record, expiresAtMs: NOW + 3600_001}),
    record => ({...record, refreshToken: PRIVATE}), record => ({...record, accessToken: PRIVATE}),
    record => ({...record, accessToken: jwt({client_id: "wrong"})}), record => ({...record, accessToken: jwt({scope: "openid analysis/read"})}),
    record => ({...record, accessToken: jwt({token_use: "id"})}), record => ({...record, accessToken: jwt({exp: Number.MAX_SAFE_INTEGER})}),
    record => ({...record, accessToken: jwt({exp: null})}), record => ({...record, accessToken: jwt({exp: NOW / 1000})}),
  ];
  for (const mutate of cases) {
    const f = fixture(), value = mutate(JSON.parse(f.data.get(f.key)));
    f.data.set(f.key, typeof value === "string" ? value : JSON.stringify(value));
    assert.equal(readWebSession(config, f.options), null);
    assert.equal(f.data.has(f.key), false);
    assert.deepEqual([...f.data], [["dashboard.preference", "preserve"]]);
  }
});

test("Namespaces separate deployments and clearing preserves unrelated or other-client data", () => {
  const f = fixture();
  const other = {...config, cognitoDomain: "https://other.auth.us-east-1.amazoncognito.com"};
  assert.equal(readWebSession(other, f.options), null);
  writeWebSession(other, f.session, f.options);
  clearWebSession(config, f.options);
  assert.equal(readWebSession(config, f.options), null);
  assert.deepEqual(readWebSession(other, f.options), f.session);
  assert.equal(f.data.get("dashboard.preference"), "preserve");
  assert.equal(readWebSession({enabled: false}), null);
});

test("Blocked storage uses fixed errors and never falls back to local or refresh-token persistence", () => {
  const safe = error => error.code === "AUTH_UNAVAILABLE" && !error.message.includes(PRIVATE);
  const f = fixture();
  assert.throws(() => readWebSession(config, {storage: {...f.storage, getItem() {throw new Error(PRIVATE);}}}), safe);
  assert.throws(() => clearWebSession(config, {storage: {...f.storage, removeItem() {throw new Error(PRIVATE);}}}), safe);
  assert.throws(() => writeWebSession(config, f.session, {...f.options, storage: {...f.storage, setItem() {throw new Error(PRIVATE);}}}), safe);
  assert.equal(f.data.has(f.key), false);
  assert.throws(() => writeWebSession(config, {...f.session, refreshToken: PRIVATE}, f.options), safe);
});
