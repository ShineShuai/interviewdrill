## rust-own | Explain ownership, borrowing and lifetimes in Rust.
- Each value has exactly one owner; it is dropped when the owner leaves scope
- Assignment moves ownership; Copy types are duplicated instead
- Borrow rules: many &T or one &mut T, never both: no aliasing mutation or data races
- Lifetimes describe how long references stay valid; elision lets the compiler infer most

## rust-send-sync | What are Send and Sync?
- Send: a value can be moved to another thread
- Sync: &T can be shared between threads (T: Sync iff &T: Send)
- Rc is neither; Arc<Mutex<T>> is the usual shared-state pattern
