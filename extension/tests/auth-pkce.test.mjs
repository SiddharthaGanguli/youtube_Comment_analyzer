import assert from "node:assert/strict";
import test from "node:test";
import {createAuthorizationRequest, consumeAuthorizationCallback, pkceChallenge} from "../auth-pkce.mjs";

const config = {extensionId: "a".repeat(32), clientId: "fixtureclient1",
  cognitoDomain: "https://comment-lens-fixture.auth.us-east-1.amazoncognito.com"};
const callback = (pending, extra = {}) => `${pending.redirectUri}?${new URLSearchParams({state: pending.state, code: "fixture-code", ...extra})}`;

test("S256 matches RFC 7636 and rejects weak or malformed verifiers", async () => {
  assert.equal(await pkceChallenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  for (const bad of ["short", "!".repeat(43), null, "a".repeat(129)]) await assert.rejects(pkceChallenge(bad));
});

test("Every authorization request uses fresh state, S256, code flow and exact callbacks", async () => {
  const first = await createAuthorizationRequest(config, 1000), second = await createAuthorizationRequest(config, 1000);
  const url = new URL(first.url);
  assert.equal(url.searchParams.get("response_type"), "code");
  assert.equal(url.searchParams.get("code_challenge_method"), "S256");
  assert.equal(url.searchParams.get("code_challenge"), await pkceChallenge(first.pending.verifier));
  assert.equal(url.searchParams.get("redirect_uri"), `https://${config.extensionId}.chromiumapp.org/cognito`);
  assert.equal(url.searchParams.get("scope"), "openid analysis/read analysis/write");
  assert.notEqual(first.pending.state, second.pending.state);
  assert.notEqual(first.pending.verifier, second.pending.verifier);
  assert.ok(!first.url.includes(first.pending.verifier));
});

test("Valid callbacks produce one-use form requests and clear the saved verifier", async () => {
  const {pending} = await createAuthorizationRequest(config, 1000), verifier = pending.verifier;
  const exchange = consumeAuthorizationCallback(callback(pending), pending, 1100), body = new URLSearchParams(exchange.options.body);
  assert.equal(body.get("grant_type"), "authorization_code");
  assert.equal(body.get("code_verifier"), verifier);
  assert.equal(body.get("client_id"), config.clientId);
  assert.equal(exchange.endpoint, `${config.cognitoDomain}/oauth2/token`);
  assert.equal(exchange.options.redirect, "error");
  assert.equal(pending.verifier, "");
  assert.throws(() => consumeAuthorizationCallback(callback(pending), pending, 1200));
});

test("Reject state mismatch, duplicate parameters, wrong origins/paths, fragments and implicit tokens", async () => {
  const {pending} = await createAuthorizationRequest(config, 1000);
  const good = callback(pending);
  for (const bad of [callback(pending, {state: "wrong"}), `${good}&state=${pending.state}`, `${good}&code=second`,
    good.replace("chromiumapp.org", "chromiumapp.org.evil.test"), good.replace("/cognito?", "/different?"), `${good}#token`,
    `${good}&access_token=private`, good.replace("https://", "http://"), callback(pending, {code: " "})]) {
    assert.throws(() => consumeAuthorizationCallback(bad, {...pending}, 1100));
  }
  assert.throws(() => consumeAuthorizationCallback(good, {...pending}, 301001));
  assert.throws(() => consumeAuthorizationCallback(good, {...pending}, 999));
});

test("Provider errors are generic and consumed; unsafe endpoint/configuration cannot receive secrets", async () => {
  const {pending} = await createAuthorizationRequest(config, 1000);
  const error = `${pending.redirectUri}?${new URLSearchParams({state: pending.state, error: "access_denied", error_description: "private details"})}`;
  assert.throws(() => consumeAuthorizationCallback(error, pending, 1100), {message: "Sign-in was not completed"});
  assert.equal(pending.consumed, true);
  for (const update of [{extensionId: "*"}, {clientId: ""}, {cognitoDomain: "https://evil.test"},
    {cognitoDomain: `${config.cognitoDomain}/path`}, {cognitoDomain: `${config.cognitoDomain}?token=secret`}]) {
    await assert.rejects(createAuthorizationRequest({...config, ...update}));
  }
  const request = await createAuthorizationRequest(config, 1000);
  assert.throws(() => consumeAuthorizationCallback(callback(request.pending), {...request.pending, tokenEndpoint: "https://evil.test/oauth2/token"}, 1100));
});
