## algo-hash | How does a hash table work? Cover collisions, resizing and worst case.
- Hash maps key to a bucket index; collisions resolved by chaining or open addressing
- Load-factor threshold triggers a rehash into a bigger table: amortized O(1) insert
- Worst case O(n) with adversarial keys; mitigate with keyed/randomized hash or tree buckets

## algo-approach | Walk through how you approach an unfamiliar coding problem.
- Clarify inputs, constraints and edge cases; restate the problem
- State a brute-force solution and its complexity, then optimize with a known pattern
- Think aloud, test on examples, finish with time and space analysis

## algo-sort | Compare quicksort, mergesort and heapsort. What do real libraries use?
- Quicksort: average O(n log n), worst O(n^2) with bad pivots, in place, cache friendly; random or median-of-three pivot
- Mergesort: O(n log n) always, stable, needs O(n) extra space; good for linked lists and external sorting
- Heapsort: O(n log n) worst case, in place, not stable, poor locality
- C++ std::sort is introsort (quicksort that falls back to heapsort); Python and Java object sorts use timsort (stable, exploits runs)
- Comparison sorting cannot beat O(n log n); counting or radix sort can for bounded keys

## algo-binary-search | What are the pitfalls of binary search? How do you search on the answer?
- Keep a clear invariant, e.g. the answer is in [lo, hi); decide the loop condition and the update with it
- mid = lo + (hi - lo) / 2 avoids integer overflow
- lower_bound and upper_bound handle duplicates and insertion points
- Binary search on the answer: a monotone predicate (feasible or not) lets you search capacity, speed or time
- Off-by-one errors: test with 0, 1 and 2 elements

## algo-window | Explain two pointers and sliding window. When do they apply?
- Two pointers: sorted array pair sums, partitioning, merging, removing duplicates in place
- Sliding window: a contiguous subarray or substring with a constraint; grow the right end, shrink the left until valid
- Each pointer moves forward only, so the work is O(n) instead of O(n^2)
- Needs a property that makes shrinking safe (non-negative values, or counts in a map)

## algo-dp | How do you recognize and solve a dynamic programming problem?
- Signals: optimal substructure and overlapping subproblems (count, min, max, can-we)
- Define the state precisely, the transition, the base cases and the order of evaluation
- Top-down memoization is quick to write; bottom-up tabulation avoids recursion limits and can compress memory to a row
- Classics: knapsack, longest common subsequence, longest increasing subsequence in O(n log n), edit distance
- State the complexity as number of states times work per transition

## algo-graph | BFS vs DFS: how do they differ and what are the uses?
- BFS uses a queue and visits by distance: shortest path in unweighted graphs, level order, O(V + E)
- DFS uses a stack or recursion: reachability, connected components, cycle detection, topological order
- Track visited to avoid cycles; mark when pushing in BFS so nodes are not queued twice
- Recursion depth can overflow on deep graphs: iterate with an explicit stack
- Choose the representation: adjacency list for sparse graphs, matrix for dense or small ones

## algo-shortest-path | Dijkstra, Bellman-Ford, A*, Floyd-Warshall: what is each for?
- Dijkstra: non-negative weights, priority queue, O((V + E) log V); finalizes a node when popped
- Bellman-Ford: negative edges and negative cycle detection, O(V * E)
- A*: Dijkstra guided by an admissible heuristic that never overestimates; fewer nodes expanded
- Floyd-Warshall: all pairs, O(V^3)
- Unweighted graph: plain BFS; DAG: relax in topological order in O(V + E)

## algo-toposort | How does topological sort work and how does it detect cycles?
- Only for DAGs: order nodes so each edge goes forward (build systems, task scheduling, course prerequisites)
- Kahn: repeatedly remove nodes with indegree 0; if fewer than V nodes are removed, there is a cycle
- DFS: push a node after visiting its children and reverse the result; a back edge means a cycle
- Both run in O(V + E)

## algo-union-find | What is union-find and what are its optimizations?
- Tracks disjoint sets: find returns the representative, union merges two sets
- Path compression flattens trees during find; union by rank or size keeps them shallow
- Together the cost is nearly constant per operation (inverse Ackermann)
- Uses: connected components, Kruskal's minimum spanning tree, cycle detection in undirected graphs

## algo-heap | How does a heap work? How do you get the top k elements?
- Binary heap in an array: parent at i, children at 2i+1 and 2i+2; push and pop are O(log n), peek is O(1)
- Building a heap from n items takes O(n) with bottom-up heapify
- Top k largest: keep a min-heap of size k, O(n log k); quickselect gives average O(n)
- Merge k sorted lists with a heap of the current heads: O(N log k)
- Python heapq is a min-heap; C++ priority_queue is a max-heap by default

## algo-lru | Design an LRU cache with O(1) get and put.
- Hash map from key to a node of a doubly linked list ordered by recency
- get: move the node to the front; put: insert at the front, and evict the tail when over capacity
- List and map updates are both O(1); head and tail sentinel nodes avoid special cases
- Python OrderedDict or C++ list plus unordered_map implement it directly
- Thread safety: one lock, or sharded caches; LFU, TTL and size-aware variants are common follow-ups

## algo-trie | When do you use a trie instead of a hash map?
- Tree keyed by characters: insert, lookup and prefix search cost O(length), independent of the number of keys
- Hash maps cannot answer prefix queries; tries give autocomplete and longest-prefix match (IP routing)
- Cost: memory per node; compress with radix trees or store children in sorted arrays
- Sorted traversal gives lexicographic order for free

## algo-amortized | What is amortized analysis? Why is dynamic array append O(1)?
- Average cost per operation over a sequence of operations, not an average over random input
- Dynamic array doubles its capacity when full: the copy costs O(n) but happens after n cheap appends, so fewer than 2 copies per element in total
- Growth factor trades memory waste against copy frequency
- Same idea: hash table rehash, union-find, splay trees
- A single operation can still be slow, which matters for latency-sensitive code

## algo-balanced-trees | Why do we need balanced trees? Compare AVL, red-black trees and B-trees.
- Plain BST degrades to O(n) on sorted input; balancing keeps height O(log n)
- AVL: stricter balance, faster lookups, more rotations on update; red-black: looser balance, cheaper updates (C++ std::map, Java TreeMap)
- B-tree: wide nodes sized to a disk page or cache line, few levels, used by databases and file systems
- Ordered structures support range queries and predecessor lookups that hash maps cannot

## algo-backtracking | Explain backtracking with pruning. Examples?
- Build a candidate step by step, undo the last choice when it cannot lead to a solution
- Template: choose, recurse, un-choose; track the used state incrementally
- Pruning (constraint checks, bounds, sorted input to skip duplicates) cuts the search space
- Examples: permutations, subsets, N-Queens, sudoku, word search
- Complexity is exponential in the worst case: state it, and mention DP when subproblems repeat

## algo-bits | Which bit tricks should you know?
- x & (x - 1) clears the lowest set bit; x & -x isolates it; popcount counts set bits
- Power of two: x > 0 and (x & (x - 1)) == 0
- XOR of all elements finds the single unpaired one; a ^ a = 0
- Bitmasks represent subsets: iterate over 0..2^n - 1 for n up to about 20
- Beware signed shifts and overflow; use unsigned types in C and C++

## algo-streaming | How do you process data that does not fit in memory? Name streaming structures.
- Reservoir sampling: uniform sample of k items from a stream of unknown length in O(k) memory
- Count-min sketch: approximate frequencies and heavy hitters; counts never underestimate
- HyperLogLog: approximate distinct count in kilobytes; Bloom filter: set membership with false positives, no false negatives
- External merge sort and map-reduce style partitioning handle exact answers on huge files
- State the accuracy and memory trade-off explicitly

## algo-intervals | How do you solve interval problems such as merging or room scheduling?
- Sort by start time, then sweep and merge while the next start is at most the current end
- Meeting rooms: sort starts and ends separately, or use a min-heap of end times; the answer is the maximum overlap
- Sweep line: turn intervals into +1 and -1 events and process them in order
- Greedy: to keep the most non-overlapping intervals, sort by end time
- Edge cases: touching endpoints, empty input, identical intervals
