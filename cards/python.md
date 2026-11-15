## py-gil | What is the GIL, what does it limit, and how do you get parallelism?
- CPython mutex: one thread executes Python bytecode at a time
- Limits CPU-bound threads; blocking I/O releases it, so threads still help for I/O
- Parallelism: multiprocessing, subprocesses, C extensions that release the GIL (NumPy)
- A free-threaded build (PEP 703) exists as an optional alternative

## py-gen | How do generators work and when do you prefer them over lists?
- A function with yield returns an iterator and suspends its state between next() calls
- Lazy, O(1) memory, good for streaming pipelines; single pass only
- Also send(), close(), yield from; async generators for async iteration

## py-default | Why is `def f(x=[])` a bug and how do you fix it?
- Default is evaluated once at definition time and shared by all calls
- Mutations leak across calls
- Fix: x=None, then `if x is None: x = []`
