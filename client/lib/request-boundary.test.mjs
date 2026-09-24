import assert from "node:assert/strict";
import test from "node:test";
import { requestBoundaryError } from "./request-boundary.mjs";

function request(headers = {}, method = "GET") {
  return { url: "http://localhost:3000/api/health", method, headers: new Headers({ host: "127.0.0.1:3000", ...headers }) };
}

test("frontend boundary accepts loopback ports and exact configured public origin", () => {
  for (const host of ["localhost:3000", "127.0.0.1:43210", "localhost", "127.0.0.1"]) {
    assert.equal(requestBoundaryError(request({ host, origin: `http://${host}` })), null);
  }
  assert.equal(requestBoundaryError(request({ host: "devflow.example:8443", origin: "https://devflow.example:8443" }), "https://devflow.example:8443"), null);
  assert.equal(requestBoundaryError(request({ "x-devflow-request": "1", origin: "http://127.0.0.1:3000" }, "PUT")), null);
  assert.equal(requestBoundaryError(request({}, "GET")), null); // Direct patch download/CLI reads.
});

test("frontend boundary rejects rebinding and forged forwarding headers", () => {
  for (const host of ["evil.example:3000", "backend:8000", "localhost.evil.example", "127.1:3000",
    "2130706433:3000", "localhost.:3000", "user@localhost:3000", "localhost:3000/path", "localhost:0", "localhost:65536", "localhost:3000,evil.example"]) {
    assert.equal(requestBoundaryError(request({ host, "x-forwarded-host": "127.0.0.1:3000" })), "host_denied", host);
  }
  assert.equal(requestBoundaryError(request({ host: "devflow.example:8080" }), "https://devflow.example"), "host_denied");
  const missing = request(); missing.headers.delete("host");
  assert.equal(requestBoundaryError(missing), "host_denied");
});

test("frontend boundary rejects foreign origins, cross-site metadata, and unmarked mutations", () => {
  for (const origin of ["null", "http://evil.example", "http://localhost:3000", "http://127.0.0.1:3001", "https://127.0.0.1:3000"]) {
    assert.equal(requestBoundaryError(request({ origin, "x-devflow-request": "1" }, "POST")), "origin_denied", origin);
  }
  for (const site of ["cross-site", "same-site"]) {
    assert.equal(requestBoundaryError(request({ "sec-fetch-site": site })), "origin_denied");
  }
  for (const method of ["POST", "PUT", "DELETE", "PATCH"]) {
    assert.equal(requestBoundaryError(request({}, method)), "csrf_denied");
  }
  assert.equal(requestBoundaryError(request(), "not a URL"), "invalid_public_origin");
});
