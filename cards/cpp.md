# C/C++ cards: `## id | question`, body = reference points
## cpp-raii | What is RAII and why does it matter for exception safety?
- Acquire in constructor, release in destructor; resource lifetime tied to scope
- Stack unwinding runs destructors on throw, so no leaks and no try/finally
- Examples: unique_ptr, lock_guard, fstream
- Destructors must not throw (implicitly noexcept; throwing during unwinding terminates)

## cpp-move | Explain move semantics. When is a move constructor not generated implicitly?
- Move transfers resources from an rvalue; source stays valid but unspecified
- std::move is only a cast to rvalue reference; it moves nothing by itself
- No implicit move ctor if the class declares copy ctor, copy/move assignment or destructor
- Mark moves noexcept so std::vector moves instead of copies on reallocation

## cpp-vtable | How does virtual dispatch work and what does it cost?
- Polymorphic object holds a vptr to a per-class vtable of function pointers
- Call = load vptr, load slot, indirect call; blocks inlining, can mispredict
- Base class needs a virtual destructor when deleting through a base pointer
- Virtual calls inside constructors/destructors resolve to the class under construction

## cpp-memorder | Explain acquire/release memory ordering in C++ atomics.
- Release store: earlier writes become visible to a thread that acquire-loads that value
- Acquire load: later reads/writes cannot be reordered before it
- The pair creates happens-before; relaxed gives atomicity only
- seq_cst adds a single total order; costlier on ARM, cheap loads on x86

## c-undefined | Name common undefined behavior in C/C++ and how to catch it.
- Signed overflow, out-of-bounds access, use-after-free, null dereference, data race
- Strict aliasing violations, uninitialized reads, shift by >= width
- Compiler may assume UB never happens and optimize on that assumption
- Tools: ASan, UBSan, TSan, MSan, valgrind, -Wall -Wextra, static analyzers
