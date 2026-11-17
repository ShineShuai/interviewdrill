/** Points to tick when grading yourself (mirrors checklist() in drill.py). */
export function checklist(cardId: string, ref: string): string[] {
  if (cardId.startsWith("bug:")) {
    return ["Named the bug class", "Explained the root cause", "Gave a fix that works"];
  }
  if (cardId.startsWith("problem:")) {
    return [
      "Right algorithm or approach", "Stated time and space complexity",
      "Covered the edge cases", "Avoided the known wrong approaches",
    ];
  }
  const bullets = ref
    .split("\n")
    .filter((ln) => ln.startsWith("- "))
    .map((ln) => ln.slice(2).trim())
    .filter(Boolean);
  return bullets.length ? bullets : [ref.trim()];
}
