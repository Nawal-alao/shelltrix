//! The bridge between asyncio and Tokio.
//!
//! matrix-sdk is async and owns a Tokio runtime. shelltrix is a Textual app
//! running on Python's asyncio. Putting the two in one process is the whole
//! trick of this migration, and there is exactly one safe way to do it:
//!
//! - The runtime lives on a **dedicated thread**, started once, parked
//!   forever. It is never built on a Python thread, and Python's event loop
//!   never drives it.
//! - Python calls into it with [`block_on`], from an ordinary thread, with the
//!   GIL released. The caller parks; the runtime thread does the work.
//!
//! The alternatives are the known ways to deadlock a GUI: nesting a runtime
//! inside asyncio's thread, or holding the GIL while the core waits on I/O —
//! which freezes the UI even when nothing is deadlocked.
//!
//! [`pyo3_async_runtimes`] solves the same problem and is worth revisiting if
//! we need cancellation or many concurrent calls; one `block_on` per call is
//! enough for now.

use std::future::Future;
use std::sync::OnceLock;

use tokio::runtime::Handle;

static RUNTIME: OnceLock<Handle> = OnceLock::new();

/// The shared runtime, started on its own thread on first use.
fn runtime() -> &'static Handle {
    RUNTIME.get_or_init(|| {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .thread_name("shelltrix-core")
            .build()
            .expect("shelltrix-core: the Tokio runtime must build");

        let handle = runtime.handle().clone();
        std::thread::Builder::new()
            .name("shelltrix-core-rt".to_owned())
            .spawn(move || runtime.block_on(std::future::pending::<()>()))
            .expect("shelltrix-core: the runtime thread must start");

        handle
    })
}

/// Runs `future` to completion on the shared runtime and returns its output.
///
/// # Panics
///
/// If called from inside a Tokio runtime context. Python threads never are
/// one; `py.allow_threads` in the `#[pyfunction]` wrappers guarantees the
/// caller is a plain thread.
pub fn block_on<F: Future>(future: F) -> F::Output {
    runtime().block_on(future)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The bridge must work from an ordinary thread, which is what every
    /// Python call site is. If this deadlocks, the UI would freeze on the
    /// first message.
    #[test]
    fn runs_a_future_from_a_plain_thread() {
        assert_eq!(block_on(async { 1 + 1 }), 2);
    }

    /// Two calls must share one runtime rather than racing to build two.
    #[test]
    fn reuses_the_same_runtime_across_calls() {
        let first = block_on(async { std::thread::current().name().unwrap().to_owned() });
        let second = block_on(async { std::thread::current().name().unwrap().to_owned() });
        assert_eq!(first, second, "the runtime must be started exactly once");
    }

    #[test]
    fn does_not_block_the_calling_thread() {
        // A Python thread blocked in `block_on` is a thread with the GIL
        // released, so the UI keeps repainting while a `/sync` is in flight.
        let worker = std::thread::spawn(|| {
            block_on(async {
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                "done"
            })
        });
        assert_eq!(worker.join().unwrap(), "done");
    }
}
