/**
 * Use Overtone from TypeScript (Node 20+ or the browser) with the typed client.
 *
 *     overtone serve                        # in backend/, first
 *     npx tsx examples/node_client.ts
 *
 * Set OVERTONE_API_URL / OVERTONE_API_KEY to point at another server.
 */

import { createClient } from "@overtone/client";

const overtone = createClient({
  baseUrl: process.env.OVERTONE_API_URL ?? "http://127.0.0.1:8787",
  apiKey: process.env.OVERTONE_API_KEY,
});

async function main() {
  const health = await overtone.health();
  console.log(`Server: Overtone ${health.version}, model ${health.model}`);

  const notebook = await overtone.notebooks.create({ title: "Client example" });
  try {
    await overtone.sources.add(notebook.id, {
      type: "text",
      title: "Launch notes",
      text:
        "The Harbor Ferry line launched in March 2025 and carried 310,000 passengers in its first ten months, " +
        "well above the forecast of 220,000. Two additional sailings were added in September to relieve " +
        "crowding on the morning crossings. Ferry riders were the most satisfied group in the autumn survey.",
    });

    process.stdout.write("\nA: ");
    for await (const event of overtone.chat.stream(notebook.id, { message: "How did the ferry do against its forecast?" })) {
      if (event.type === "text") process.stdout.write(event.delta);
      if (event.type === "error") throw new Error(event.error);
      if (event.type === "done") {
        console.log("\n");
        for (const citation of event.message.citations ?? []) console.log(`  [${citation.index}] ${citation.sourceTitle}`);
      }
    }

    const episode = await overtone.artifacts.generate(
      notebook.id,
      { kind: "audio_overview", options: { format: "solo", length: "short" } },
      { onProgress: (artifact) => artifact.progress && console.log(`  ... ${artifact.progress}`) },
    );
    console.log(`Episode: ${episode.title} (${episode.script?.turns.length ?? 0} turns)`);
    console.log(episode.audio ? `Audio: ${overtone.artifacts.audioUrl(episode.id)}` : "No rendered audio (no speech key on the server).");
  } finally {
    await overtone.notebooks.delete(notebook.id);
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
