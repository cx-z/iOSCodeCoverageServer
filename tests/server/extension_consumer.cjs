"use strict";
// Checks the real extension consumer without copying its contract into server tests.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const contracts = require(path.join(process.argv[2], "src/shared/contracts.js"));
const api = require(path.join(process.argv[2], "src/background/api-client.js"));
const { summary, details } = JSON.parse(fs.readFileSync(0, "utf8"));

async function verify() {
  // Keep summary, file descriptors and annotations bound through the actual API client.
  contracts.validatePresentationResolver(summary);
  const fetch = async (_url, options) => {
    const request = JSON.parse(options.body);
    return { ok: true, status: 200, json: async () =>
      request.file_id ? details[request.file_id] : summary };
  };
  const settings = { serverOrigin: "http://127.0.0.1:8765" };
  const mr = { origin: `https://${summary.mr.host}`, host: summary.mr.host,
    projectPath: summary.mr.project_path, iid: summary.mr.iid,
    pathname: `/${summary.mr.project_path}/merge_requests/${summary.mr.iid}/diffs` };
  const result = await api.resolveMergeRequest(fetch, settings, { mr, pageVersion: summary.page_version });
  assert.equal(result.canAnnotate, true);
  assert.equal(result.presentationId, summary.presentation_id);
  assert.deepEqual(result.summary, summary.projection.summary);
  for (const file of summary.projection.files) {
    contracts.validatePresentationFile(details[file.file_id]);
    const detail = await api.loadPresentationFile(fetch, settings, result, file.file_id);
    assert.deepEqual(detail.file, file);
  }
  // A malformed count must still be rejected; compatibility does not relax validation.
  if (summary.projection.files.length) {
    const invalid = structuredClone(summary);
    invalid.projection.files[0].uncovered += 1;
    assert.throws(() => contracts.validatePresentationResolver(invalid));
  }
  process.stdout.write("accepted\n");
}
verify().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
