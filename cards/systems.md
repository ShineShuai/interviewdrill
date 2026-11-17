## os-vmem | How does virtual memory work? What happens on a page fault?
- Per-process page tables map virtual to physical pages; MMU with TLB caches translations
- Fault = page not present or permission violation; the CPU traps into the kernel
- Valid but not resident: allocate zero page, load from disk, or copy-on-write; invalid: SIGSEGV
- Benefits: isolation, lazy allocation, mmap, shared libraries

## net-url | What happens when you type a URL and press Enter (network part)?
- DNS lookup, TCP handshake (SYN, SYN-ACK, ACK), TLS handshake, HTTP request/response
- TLS 1.3 needs 1 RTT; HTTP/3 runs over QUIC (UDP) and avoids TCP head-of-line blocking
- Caching, CDN, connection reuse reduce latency

## net-congestion | How does TCP congestion control work?
- cwnd limits bytes in flight; slow start (exponential) until ssthresh, then additive increase
- Loss: multiplicative decrease (Reno halves, CUBIC 0.7); 3 dup ACKs trigger fast retransmit
- BBR models bandwidth and RTT instead of using loss as the signal

## os-process-thread | Process vs thread. What does a context switch cost?
- Process: own address space and file descriptors; thread: shares memory with its process, own stack and registers
- Thread switch is cheaper (same page tables); process switch changes the address space (TLB flush or ASID)
- Direct cost: saving and restoring registers; hidden cost: cold caches and TLB afterwards
- Threads need synchronization for shared state; a crash in one thread takes down the process

## os-sync | Mutex, semaphore, condition variable, spinlock: when do you use which? What is a deadlock?
- Mutex: exclusive ownership, sleeps on contention; spinlock: busy-waits, only for very short critical sections
- Semaphore: counter for N resources or signaling; condition variable: wait for a predicate while holding a mutex
- Always re-check the predicate in a loop (spurious wakeups)
- Deadlock needs mutual exclusion, hold and wait, no preemption and circular wait
- Prevent it with a global lock order, try-lock with back-off, or fewer locks

## os-io-models | Blocking, non-blocking, select/poll, epoll and io_uring: how does a server handle 10k connections?
- Thread per connection costs memory and context switches; it does not scale to 10k+
- select/poll scan every descriptor on each call: O(n); epoll (Linux) and kqueue (BSD, macOS) return only ready ones
- Event loop: non-blocking sockets plus readiness notification, a few threads, state machines or coroutines
- io_uring: submit and complete I/O through shared ring buffers, fewer syscalls, true async file I/O
- Go and Rust async runtimes hide this behind goroutines or futures on top of epoll/kqueue

## os-cache | How do CPU caches affect performance? What is false sharing?
- Memory moves in cache lines (typically 64 bytes); locality and sequential access matter more than instruction count
- Array of structs vs struct of arrays: pick the layout that matches the access pattern
- False sharing: independent variables on one cache line written by different cores bounce the line between them
- Fix: pad or align hot per-thread data to the cache line size, or keep per-thread copies
- Measure with perf counters (cache misses), not guesses

## os-memory-model | What are atomics and memory ordering? What is a data race?
- Data race: unsynchronized access to one location, at least one write; undefined behavior in C/C++, a bug in Go
- Atomics make single operations indivisible; memory ordering decides what other threads may observe
- Sequentially consistent is the safe default; acquire/release pair a publish with a consume; relaxed gives atomicity only
- Compilers and CPUs reorder, so a plain flag is not a safe signal; use a mutex, atomic or channel
- Tools: ThreadSanitizer, Go's -race

## net-tcp-udp | TCP vs UDP. When do you pick each? What does QUIC change?
- TCP: reliable, ordered byte stream, flow and congestion control; head-of-line blocking after one loss
- UDP: datagrams without delivery guarantees; the application adds what it needs
- UDP fits real-time media, games, DNS, and custom protocols
- QUIC: built on UDP, streams without cross-stream blocking, TLS 1.3 built in, 0-RTT resume, connection migration
- HTTP/3 runs on QUIC

## net-http | What changed between HTTP/1.1, HTTP/2 and HTTP/3?
- HTTP/1.1: text, one request at a time per connection; browsers open several connections
- HTTP/2: binary framing, multiplexed streams over one TCP connection, header compression (HPACK); TCP head-of-line blocking remains
- HTTP/3: same semantics over QUIC, per-stream loss recovery, faster setup, better on mobile networks
- Caching, range requests and cookies work the same, which is why video segments travel over any of them

## net-tls | How does a TLS 1.3 handshake work and what does a certificate prove?
- Client hello with key share and supported ciphers; server replies with its key share, certificate and signature; 1 round trip
- Keys derived by ephemeral Diffie-Hellman, so past traffic stays safe if the server key leaks (forward secrecy)
- Certificate binds a domain to a public key; the client verifies the chain to a trusted CA, the name and validity
- Session resumption and 0-RTT save a round trip; 0-RTT data is replayable
- SNI says which hostname the client wants before encryption starts

## net-dns | How does DNS resolution work? How does it help with load balancing and failover?
- Stub resolver asks a recursive resolver, which walks root, TLD and authoritative servers and caches by TTL
- Records: A/AAAA, CNAME, NS, MX, TXT; caching makes changes slow to spread, so lower the TTL before a migration
- Round robin, geo or latency-based answers steer clients to regions or CDN edges
- Anycast gives one IP served from many locations
- Weaknesses: stale caches, no health awareness in plain round robin

## sys-caching | Describe caching strategies and the hard problems that come with them.
- Cache-aside: app reads cache, on miss loads from the store and fills it; write-through writes both; write-back writes the store later
- Eviction: LRU, LFU, TTL; size the cache to the working set
- Invalidation: TTL, explicit delete on write, versioned keys; stale reads are a trade-off to state out loud
- Stampede: many misses hit the origin at once; use request coalescing, locking, or early refresh
- Hot keys and cold start need special handling

## sys-consistency | What does CAP say? Explain strong vs eventual consistency and quorums.
- During a network partition you choose between consistency and availability; without a partition you trade latency for consistency
- Strong (linearizable): reads see the latest write; eventual: replicas converge if writes stop
- Quorum: N replicas, write to W, read from R; W + R > N means reads overlap the latest write
- Consensus (Raft, Paxos) orders writes through a leader and majority
- Pick per use case: payments need strong, view counters can be eventual

## sys-load-balancing | How does load balancing work? L4 vs L7, algorithms, health checks.
- L4 balances connections by IP and port (fast, protocol agnostic); L7 reads HTTP and can route by path, header or cookie
- Algorithms: round robin, least connections, weighted, power of two choices, consistent hashing for cache affinity
- Health checks and outlier ejection remove bad backends; connection draining on deploys
- The balancer itself must not be a single point of failure: pairs, anycast or DNS in front

## sys-consistent-hashing | What is consistent hashing and what problem does it solve?
- Naive hash(key) mod N remaps almost every key when N changes
- Consistent hashing puts nodes and keys on a ring; a key belongs to the next node clockwise, so only about 1/N keys move
- Virtual nodes smooth the load and allow weighting
- Used by caches, sharded databases and CDN request routing; replicas are the next nodes on the ring

## sys-queues | Message queues: delivery guarantees, idempotency and backpressure.
- Queues decouple producers from consumers and absorb bursts; Kafka is a partitioned log, SQS-style queues hand out messages
- At-most-once may lose, at-least-once may duplicate, exactly-once effects need idempotent consumers or transactions
- Idempotency key or dedupe table makes retries safe; failed messages go to a dead-letter queue
- Ordering holds only within a partition or key
- Backpressure: bounded queues, rate limits, shedding load instead of growing forever

## sys-rate-limit | Design a rate limiter. Compare token bucket, leaky bucket and sliding window.
- Token bucket: refill at rate r up to capacity b; allows bursts of b, average r; O(1) state per key
- Leaky bucket: constant outflow, smooths bursts into a steady rate
- Fixed window is cheap but lets double bursts at window edges; sliding window log or counter is accurate and costs more
- Distributed: central store (Redis with atomic scripts) or per-node limits with a shared budget
- Return 429 with Retry-After; limit by user, key and IP

## sys-db | Explain indexes, transactions and isolation levels.
- B-tree index: sorted pages, O(log n) lookup and range scans; costs writes and space; composite index order follows the leftmost-prefix rule
- ACID: atomic, consistent, isolated, durable (write-ahead log)
- Isolation levels: read committed, repeatable read, serializable; weaker ones allow non-repeatable reads, phantoms or write skew
- MVCC lets readers not block writers
- Check query plans (EXPLAIN) before adding an index

## sys-tail-latency | Why does p99 latency matter and how do you reduce it?
- Averages hide slow requests; a page that fans out to 100 calls hits the p99 of one almost every time
- Causes: queueing, GC pauses, noisy neighbors, retries, cold caches, lock contention
- Fixes: timeouts and budgets, hedged requests, load shedding, smaller batches, isolation of slow work
- Measure with percentiles and histograms, not means
