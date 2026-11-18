// Rasterize public/icon.svg into the PNG icons iOS and Android need (written to public/icons,
// not committed: a patch cannot carry binary files). iOS ignores SVG for the home-screen icon.
// Usage: node scripts/make-icons.mjs
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Resvg } from "@resvg/resvg-js";

/** name -> {svg, size}. The glyph must be paths, not text: fonts differ between machines. */
export function variants(svg) {
  const bg = svg.match(/<rect[^>]*fill="([^"]+)"/)?.[1];
  if (!bg || !svg.includes('viewBox="0 0 64 64"')) throw new Error("icon.svg: unexpected shape");
  const square = svg.replace(/ rx="[^"]*"/, ""); // iOS and masks round the corners themselves
  // maskable: the glyph inside the central 80% (the safe zone), the background to the edge
  const maskable = square
    .replace('viewBox="0 0 64 64"', 'viewBox="-8 -8 80 80"')
    .replace(/(<svg[^>]*>)/, `$1<rect x="-8" y="-8" width="80" height="80" fill="${bg}"/>`);
  return {
    "apple-touch-icon.png": { svg: square, size: 180 },
    "icon-192.png": { svg, size: 192 },
    "icon-512.png": { svg, size: 512 },
    "icon-maskable-512.png": { svg: maskable, size: 512 },
  };
}

export function render(svg, size) {
  return new Resvg(svg, { fitTo: { mode: "width", value: size } }).render();
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const out = join(root, "public", "icons");
  mkdirSync(out, { recursive: true });
  const svg = readFileSync(join(root, "public", "icon.svg"), "utf8");
  for (const [name, v] of Object.entries(variants(svg))) {
    writeFileSync(join(out, name), render(v.svg, v.size).asPng());
  }
  console.log(`wrote ${Object.keys(variants(svg)).length} icons into public/icons`);
}
