//! Local JSONL telemetry logging. Used to correlate client and server measurements offline. The
//! logger never blocks the caller: rows are pushed into a bounded channel and dropped if the
//! writer thread falls behind.

use parking_lot::Mutex;
use std::{
    fs,
    io::{BufWriter, Write},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
        mpsc::{Receiver, RecvTimeoutError, SyncSender, TrySendError, sync_channel},
    },
    thread::{self, JoinHandle},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

const DEFAULT_MAX_FILE_SIZE: u64 = 100 * 1024 * 1024;
const DEFAULT_MAX_SESSIONS: usize = 10;
const CHANNEL_CAPACITY: usize = 1024;
const FLUSH_INTERVAL: Duration = Duration::from_millis(500);

pub fn unix_timestamp_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

pub struct TelemetryLogger {
    sender: Mutex<Option<SyncSender<String>>>,
    writer_thread: Mutex<Option<JoinHandle<()>>>,
    dropped_rows: AtomicU64,
}

impl TelemetryLogger {
    // Creates the session directory that will contain the log files of a single streaming
    // session. One session directory per stream start keeps client and server logs paired.
    pub fn create_session_dir(base_dir: &Path) -> Option<PathBuf> {
        fs::create_dir_all(base_dir).ok()?;

        let unix_ns = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();

        // Retry with a suffix in the unlikely case of a name collision
        let session_dir = (0..100).find_map(|index| {
            let fname = if index == 0 {
                format!("session_{unix_ns}")
            } else {
                format!("session_{unix_ns}_{index}")
            };
            let session_dir = base_dir.join(fname);
            fs::create_dir(&session_dir).ok().map(|_| session_dir)
        })?;

        Self::prune_old_sessions(base_dir);

        Some(session_dir)
    }

    pub fn start(session_dir: &Path, name: &str, header_json: &str) -> Option<Arc<Self>> {
        Self::start_impl(session_dir, name, header_json, DEFAULT_MAX_FILE_SIZE)
    }

    fn start_impl(
        session_dir: &Path,
        name: &str,
        header_json: &str,
        max_file_size: u64,
    ) -> Option<Arc<Self>> {
        if !session_dir.is_dir() {
            return None;
        }

        let (sender, receiver) = sync_channel(CHANNEL_CAPACITY);

        let session_dir = session_dir.to_path_buf();
        let name = name.to_owned();
        let header_json = header_json.to_owned();

        let writer_thread = thread::Builder::new()
            .name(format!("alvr-telemetry-{name}"))
            .spawn(move || {
                writer_loop(receiver, session_dir, name, header_json, max_file_size);
            })
            .ok()?;

        Some(Arc::new(Self {
            sender: Mutex::new(Some(sender)),
            writer_thread: Mutex::new(Some(writer_thread)),
            dropped_rows: AtomicU64::new(0),
        }))
    }

    // Best effort. If the channel is full, the row is dropped and counted.
    pub fn log(&self, row_json: String) {
        let sender_guard = self.sender.lock();
        let Some(sender) = sender_guard.as_ref() else {
            return;
        };

        match sender.try_send(row_json) {
            Ok(()) => (),
            Err(TrySendError::Full(_)) => {
                self.dropped_rows.fetch_add(1, Ordering::Relaxed);
            }
            Err(TrySendError::Disconnected(_)) => (),
        }
    }

    pub fn dropped_rows(&self) -> u64 {
        self.dropped_rows.load(Ordering::Relaxed)
    }

    // Disconnects the channel and waits for the writer to flush and exit.
    pub fn stop(&self) {
        *self.sender.lock() = None;

        if let Some(thread) = self.writer_thread.lock().take() {
            thread.join().ok();
        }
    }

    fn prune_old_sessions(base_dir: &Path) {
        let Ok(entries) = fs::read_dir(base_dir) else {
            return;
        };

        let mut session_dirs: Vec<_> = entries
            .into_iter()
            .filter_map(|entry| entry.ok())
            .filter(|entry| {
                entry
                    .file_name()
                    .to_str()
                    .is_some_and(|fname| fname.starts_with("session_"))
            })
            .map(|entry| entry.path())
            .collect();
        session_dirs.sort();

        while session_dirs.len() > DEFAULT_MAX_SESSIONS {
            let oldest = session_dirs.remove(0);
            fs::remove_dir_all(oldest).ok();
        }
    }
}

impl Drop for TelemetryLogger {
    fn drop(&mut self) {
        self.stop();
    }
}

fn writer_loop(
    receiver: Receiver<String>,
    session_dir: PathBuf,
    name: String,
    header_json: String,
    max_file_size: u64,
) {
    let file_path = session_dir.join(format!("{name}.jsonl"));
    let header_line = format!("{header_json}\n");

    // Every file starts with the header line, so each file is self-describing after rotation
    let open_file = || -> Option<(BufWriter<fs::File>, u64)> {
        let mut writer = create_file(&file_path)?;
        writer.write_all(header_line.as_bytes()).ok()?;
        Some((writer, header_line.len() as u64))
    };

    let (mut writer, mut current_size) = match open_file() {
        Some(pair) => pair,
        None => return,
    };
    let mut rotation_index = 0;

    loop {
        match receiver.recv_timeout(FLUSH_INTERVAL) {
            Ok(row) => {
                let size = row.len() as u64 + 1;
                if current_size + size > max_file_size {
                    writer.flush().ok();
                    drop(writer);

                    let rotated_path = session_dir.join(format!("{name}_r{rotation_index}.jsonl"));
                    fs::rename(&file_path, rotated_path).ok();
                    rotation_index += 1;

                    let Some((new_writer, new_size)) = open_file() else {
                        return;
                    };
                    writer = new_writer;
                    current_size = new_size;
                }

                writer.write_all(row.as_bytes()).ok();
                writer.write_all(b"\n").ok();
                current_size += size;
            }
            Err(RecvTimeoutError::Timeout) => {
                writer.flush().ok();
            }
            Err(RecvTimeoutError::Disconnected) => {
                writer.flush().ok();
                return;
            }
        }
    }
}

fn create_file(file_path: &Path) -> Option<BufWriter<fs::File>> {
    let file = fs::File::create(file_path).ok()?;
    Some(BufWriter::new(file))
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn test_session_dir_pruning() {
        let base_dir = std::env::temp_dir().join(format!("alvr_telemetry_test_{}", std::process::id()));
        fs::remove_dir_all(&base_dir).ok();

        let dir1 = TelemetryLogger::create_session_dir(&base_dir).unwrap();
        let dir2 = TelemetryLogger::create_session_dir(&base_dir).unwrap();
        assert!(dir1.is_dir() && dir2.is_dir());
        assert_ne!(dir1, dir2);

        for _ in 0..(DEFAULT_MAX_SESSIONS - 2) {
            TelemetryLogger::create_session_dir(&base_dir).unwrap();
        }
        assert_eq!(fs::read_dir(&base_dir).unwrap().count(), DEFAULT_MAX_SESSIONS);

        // Creating one more session must evict the oldest one
        TelemetryLogger::create_session_dir(&base_dir).unwrap();
        assert_eq!(fs::read_dir(&base_dir).unwrap().count(), DEFAULT_MAX_SESSIONS);

        fs::remove_dir_all(&base_dir).ok();
    }

    #[test]
    fn test_logging_and_rotation() {
        let session_dir = std::env::temp_dir().join(format!("alvr_telemetry_rot_{}", std::process::id()));
        fs::remove_dir_all(&session_dir).ok();
        fs::create_dir_all(&session_dir).ok();

        let max_file_size = 128;
        let logger =
            TelemetryLogger::start_impl(&session_dir, "test", r#"{"type":"header"}"#, max_file_size)
                .unwrap();

        // 63-byte rows: the second row does not fit in a 128-byte file, so rotation must kick in
        let row = format!("\"{}\"", "x".repeat(60));
        for _ in 0..4 {
            logger.log(row.clone());
        }
        drop(logger); // disconnect the channel and flush

        let names: Vec<_> = fs::read_dir(&session_dir)
            .unwrap()
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect();

        assert!(names.iter().any(|fname| fname == "test_r0.jsonl"));
        assert!(names.iter().any(|fname| fname == "test.jsonl"));

        for fname in names.iter().filter(|fname| fname.ends_with(".jsonl")) {
            let content = fs::read_to_string(session_dir.join(fname)).unwrap();
            let lines: Vec<&str> = content.lines().collect();
            assert_eq!(lines[0], r#"{"type":"header"}"#);
            for line in &lines[1..] {
                assert_eq!(line, &row);
            }
        }

        fs::remove_dir_all(&session_dir).ok();
    }
}
