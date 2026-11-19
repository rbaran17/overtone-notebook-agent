import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { createClient, OvertoneError } from "../src/index";
import type { Source } from "../src/types";

/* ───────────────────────────── Fakes ───────────────────────────── */

interface Upload {
  url: string;
  /** Names of the `file` parts in this request. */
  files: string[];
}

type Reply = (name: string) => Response | Promise<Response>;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const added: Reply = (name) => json(201, { sources: [{ id: `src_${name}`, title: name }], errors: [] });

const refused = (status: number, code: string, message: string): Reply => (name) =>
  json(status, { error: { message, code }, sources: [], errors: [{ name, error: message, code }] });

/** A client whose fetch records each upload and answers per file name (default: added). */
function harness(replies: Record<string, Reply> = {}) {
  const uploads: Upload[] = [];
  let inFlight = 0;
  let maxInFlight = 0;
  const fakeFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    assert.ok(init?.body instanceof FormData, "uploads are multipart");
    const files = init.body.getAll("file").map((part) => (part as File).name);
    uploads.push({ url: String(input), files });
    inFlight += 1;
    maxInFlight = Math.max(maxInFlight, inFlight);
    try {
      await new Promise((resolve) => setImmediate(resolve));
      return await (replies[files[0]] ?? added)(files[0]);
    } finally {
      inFlight -= 1;
    }
  };
  const client = createClient({ baseUrl: "http://engine.test", fetch: fakeFetch as typeof fetch });
  return { client, uploads, maxInFlight: () => maxInFlight };
}

const file = (name: string, size = 4) => new File([new Uint8Array(size)], name);

const errorsOf = (result: Source[]) =>
  (result as unknown as { errors: Array<{ name: string; error: string; code: string }> }).errors;

/* ───────────────────────────── Tests ───────────────────────────── */

describe("sources.addFiles", () => {
  test("sends one request per file, in order and one at a time", async () => {
    const { client, uploads, maxInFlight } = harness();

    const result = await client.sources.addFiles("nb_1", [file("a.pdf"), file("b.md"), file("c.txt")]);

    assert.deepEqual(uploads, [
      { url: "http://engine.test/notebooks/nb_1/sources", files: ["a.pdf"] },
      { url: "http://engine.test/notebooks/nb_1/sources", files: ["b.md"] },
      { url: "http://engine.test/notebooks/nb_1/sources", files: ["c.txt"] },
    ]);
    assert.equal(maxInFlight(), 1);
    assert.deepEqual(result.map((s) => s.title), ["a.pdf", "b.md", "c.txt"]);
    assert.deepEqual(errorsOf(result), []);
  });

  test("an empty list makes no request", async () => {
    const { client, uploads } = harness();

    const result = await client.sources.addFiles("nb_1", []);

    assert.equal(uploads.length, 0);
    assert.deepEqual([...result], []);
    assert.deepEqual(errorsOf(result), []);
  });

  test("a file the engine refuses is reported and the others are still added", async () => {
    const message = '"big.pdf" is larger than the 50 MB upload limit.';
    const { client, uploads } = harness({ "big.pdf": refused(413, "too_large", message) });

    const result = await client.sources.addFiles("nb_1", [file("a.pdf"), file("big.pdf"), file("c.txt")]);

    assert.equal(uploads.length, 3);
    assert.deepEqual(result.map((s) => s.title), ["a.pdf", "c.txt"]);
    assert.deepEqual(errorsOf(result), [{ name: "big.pdf", error: message, code: "too_large" }]);
    // `errors` rides along without changing what the array looks like to callers.
    assert.deepEqual(Object.keys(result), ["0", "1"]);
  });

  test("a request that fails outright costs only its own file", async () => {
    const { client } = harness({
      "b.md": () => Promise.reject(new TypeError("fetch failed")),
      "c.txt": () => new Response("Internal Server Error", { status: 500, statusText: "Internal Server Error" }),
    });

    const result = await client.sources.addFiles("nb_1", [file("a.pdf"), file("b.md"), file("c.txt"), file("d.csv")]);

    assert.deepEqual(result.map((s) => s.title), ["a.pdf", "d.csv"]);
    assert.deepEqual(errorsOf(result), [
      { name: "b.md", error: "fetch failed", code: "request_failed" },
      { name: "c.txt", error: "Internal Server Error", code: "http_error" },
    ]);
  });

  test("throws the first failure, with its status and code, when no file was added", async () => {
    const { client, uploads } = harness({
      "a.pdf": refused(413, "too_large", '"a.pdf" is larger than the 50 MB upload limit.'),
      "b.xyz": refused(415, "unsupported_type", "Unsupported file type."),
    });

    await assert.rejects(
      client.sources.addFiles("nb_1", [file("a.pdf"), file("b.xyz")]),
      (err: unknown) => {
        assert.ok(err instanceof OvertoneError);
        assert.equal(err.status, 413);
        assert.equal(err.code, "too_large");
        assert.equal(err.message, '"a.pdf" is larger than the 50 MB upload limit.');
        return true;
      },
    );
    assert.equal(uploads.length, 2, "every file is still attempted");
  });
});
