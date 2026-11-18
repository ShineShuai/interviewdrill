import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { render, variants } from "../scripts/make-icons.mjs";

const svg = readFileSync(new URL("../public/icon.svg", import.meta.url), "utf8");
// `pixels` copies the whole buffer on every access: take it once
const decode = (v, size) => {
  const img = render(v.svg, size);
  return { width: img.width, px: img.pixels };
};
const alphaAt = (img, x, y) => img.px[(y * img.width + x) * 4 + 3];
const rgbAt = (img, x, y) => [...img.px.subarray((y * img.width + x) * 4, (y * img.width + x) * 4 + 3)];

test("the icon has no text, so every machine renders the same glyph", () => {
  assert.doesNotMatch(svg, /<text/);
});

test("renders the four PNGs at their sizes", () => {
  const sizes = { "apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512,
    "icon-maskable-512.png": 512 };
  for (const [name, v] of Object.entries(variants(svg))) {
    const img = render(v.svg, v.size);
    assert.deepEqual([img.width, img.height], [sizes[name], sizes[name]], name);
    const png = img.asPng();
    assert.equal(png.subarray(1, 4).toString(), "PNG", name);
    assert.equal(png.readUInt32BE(16), sizes[name], `${name} IHDR width`);
  }
});

test("apple and maskable icons are opaque to the corner, the rounded icons are not", () => {
  const v = variants(svg);
  const apple = decode(v["apple-touch-icon.png"], 180);
  const maskable = decode(v["icon-maskable-512.png"], 512);
  const any = decode(v["icon-512.png"], 512);
  assert.equal(alphaAt(apple, 0, 0), 255);
  assert.equal(alphaAt(maskable, 0, 0), 255);
  assert.equal(alphaAt(any, 0, 0), 0);
  assert.deepEqual(rgbAt(apple, 0, 0), [0x0a, 0x66, 0xff]);
});

test("the maskable glyph stays inside the central 80%", () => {
  const img = decode(variants(svg)["icon-maskable-512.png"], 512);
  const margin = Math.floor(512 * 0.1);
  for (let y = 0; y < 512; y++) {
    for (let x = 0; x < 512; x++) {
      if (x >= margin && x < 512 - margin && y >= margin && y < 512 - margin) continue;
      const [r, g, b] = rgbAt(img, x, y);
      assert.ok(r === 0x0a && g === 0x66 && b === 0xff, `${x},${y} is not background`);
    }
  }
  assert.ok(rgbAt(img, 256 + 80, 256).every((c) => c > 200), "white glyph stroke near the centre");
});
