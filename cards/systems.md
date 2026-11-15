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
