## go-chan | Buffered vs unbuffered channels; what causes goroutine leaks?
- Unbuffered: send blocks until a receiver is ready (synchronization point)
- Buffered: send blocks only when full, receive only when empty
- Leak: goroutine blocked forever on a channel nobody serves or closes
- Fix: context cancellation, select on ctx.Done(), sender closes the channel

## go-nil-iface | Why can an interface holding a nil pointer be != nil?
- An interface value is (dynamic type, value); it is nil only if both are nil
- Returning a typed nil pointer as `error` yields a non-nil interface
- Fix: return a literal nil explicitly

## go-sched | Describe the Go scheduler (G, M, P).
- G = goroutine, M = OS thread, P = logical processor with a local run queue
- Idle Ps steal work from other queues; an M blocked in a syscall hands off its P
- Preemptive since 1.14 (signal-based async preemption)
- Goroutines start with small growable stacks, so millions are cheap
