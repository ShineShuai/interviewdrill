export interface Card {
  id: string;
  topic: string;
  question: string;
  ref: string;
}

/**
 * Parse `## id | question` headings; body lines are the reference (same as drill.py).
 * `<!-- -->` lines are provenance and are not part of the reference.
 */
export function parseCards(topic: string, text: string): Card[] {
  return ("\n" + text)
    .split("\n## ")
    .slice(1)
    .map((block) => {
      const nl = block.indexOf("\n");
      const head = nl < 0 ? block : block.slice(0, nl);
      const body = (nl < 0 ? "" : block.slice(nl + 1))
        .split("\n")
        .filter((ln) => !ln.trimStart().startsWith("<!--"))
        .join("\n");
      const bar = head.indexOf("|");
      const id = bar < 0 ? head : head.slice(0, bar);
      const question = bar < 0 ? "" : head.slice(bar + 1);
      return { id: id.trim(), topic, question: question.trim(), ref: body.trim() };
    });
}

/** Reject cards the Worker could not show or grade: empty fields or duplicate ids. */
export function checkCards(cards: Card[]): void {
  const seen = new Set<string>();
  for (const c of cards) {
    if (!c.id || !c.topic || !c.question || !c.ref) {
      throw new Error(`incomplete card: ${JSON.stringify(c.id)}`);
    }
    if (seen.has(c.id)) throw new Error(`duplicate card id: ${c.id}`);
    seen.add(c.id);
  }
}

const quote = (s: string): string => `'${s.replaceAll("'", "''")}'`;

/** SQL that makes the cards table equal to `cards`; state of surviving ids is kept. */
export function syncSql(cards: Card[]): string {
  return [
    ...cards.map(
      (c) =>
        `INSERT INTO cards(id, topic, question, ref) VALUES(${quote(c.id)}, ${quote(c.topic)}, ` +
        `${quote(c.question)}, ${quote(c.ref)}) ON CONFLICT(id) DO UPDATE SET ` +
        "topic = excluded.topic, question = excluded.question, ref = excluded.ref;",
    ),
    `DELETE FROM cards WHERE id NOT IN (${cards.map((c) => quote(c.id)).join(", ")});`,
    "DELETE FROM card_state WHERE card_id NOT IN (SELECT id FROM cards);",
  ].join("\n");
}
