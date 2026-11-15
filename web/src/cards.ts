export interface Card {
  id: string;
  topic: string;
  question: string;
  ref: string;
}

/** Parse `## id | question` headings; the body lines are the reference (same as drill.py). */
export function parseCards(topic: string, text: string): Card[] {
  return ("\n" + text)
    .split("\n## ")
    .slice(1)
    .map((block) => {
      const nl = block.indexOf("\n");
      const head = nl < 0 ? block : block.slice(0, nl);
      const body = nl < 0 ? "" : block.slice(nl + 1);
      const bar = head.indexOf("|");
      const id = bar < 0 ? head : head.slice(0, bar);
      const question = bar < 0 ? "" : head.slice(bar + 1);
      return { id: id.trim(), topic, question: question.trim(), ref: body.trim() };
    });
}
