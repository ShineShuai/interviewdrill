## algo-hash | How does a hash table work? Cover collisions, resizing and worst case.
- Hash maps key to a bucket index; collisions resolved by chaining or open addressing
- Load-factor threshold triggers a rehash into a bigger table: amortized O(1) insert
- Worst case O(n) with adversarial keys; mitigate with keyed/randomized hash or tree buckets

## algo-approach | Walk through how you approach an unfamiliar coding problem.
- Clarify inputs, constraints and edge cases; restate the problem
- State a brute-force solution and its complexity, then optimize with a known pattern
- Think aloud, test on examples, finish with time and space analysis
