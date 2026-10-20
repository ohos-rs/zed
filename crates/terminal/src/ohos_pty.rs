use alacritty_terminal::{
    event::{OnResize, WindowSize},
    tty::{ChildEvent, EventedPty, EventedReadWrite},
};
use polling::{Event, PollMode, Poller};
use signal_hook::{
    SigId,
    low_level::{pipe, unregister},
};
use std::{
    fs::File,
    io::{self, Read},
    mem::{ManuallyDrop, MaybeUninit},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{net::UnixStream, process::CommandExt},
    },
    process::{Child, Command},
    sync::Arc,
};

// Alacritty's event loop reserves these keys for PTY I/O and child exits.
const READ_WRITE_TOKEN: usize = 0;
const CHILD_EVENT_TOKEN: usize = 1;

pub(super) struct Options {
    pub command: Command,
    pub child_signal_mask: Option<SignalMask>,
    pub drain_on_exit: bool,
}

#[derive(Clone, Copy)]
pub(super) struct SignalMask(libc::sigset_t);

impl SignalMask {
    pub fn current() -> io::Result<Self> {
        // Both sigemptyset and pthread_sigmask may only write the kernel-sized portion.
        let mut mask = MaybeUninit::<libc::sigset_t>::zeroed();
        if unsafe { libc::sigemptyset(mask.as_mut_ptr()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let result = unsafe {
            libc::pthread_sigmask(libc::SIG_SETMASK, std::ptr::null(), mask.as_mut_ptr())
        };
        if result != 0 {
            return Err(io::Error::from_raw_os_error(result));
        }
        Ok(Self(unsafe { mask.assume_init() }))
    }

    fn apply(self) -> io::Result<()> {
        if unsafe { libc::sigprocmask(libc::SIG_SETMASK, &self.0, std::ptr::null_mut()) } < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}

struct ChildSignal {
    receiver: UnixStream,
    registration: SigId,
}

impl Drop for ChildSignal {
    fn drop(&mut self) {
        unregister(self.registration);
    }
}

pub(super) struct Pty {
    file: ManuallyDrop<File>,
    child: Child,
    signals: ChildSignal,
    exited: bool,
}

impl Pty {
    pub fn file(&self) -> &File {
        &self.file
    }
    pub fn child(&self) -> &Child {
        &self.child
    }
}

fn window_size(size: WindowSize) -> libc::winsize {
    libc::winsize {
        ws_row: size.num_lines,
        ws_col: size.num_cols,
        ws_xpixel: size.num_cols.saturating_mul(size.cell_width),
        ws_ypixel: size.num_lines.saturating_mul(size.cell_height),
    }
}

pub(super) fn new(mut options: Options, size: WindowSize, window_id: u64) -> io::Result<Pty> {
    if size.num_lines == 0 || size.num_cols == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "empty PTY grid",
        ));
    }
    let mut master = -1;
    let mut slave = -1;
    let mut size = window_size(size);
    // The OHOS libc supplies the platform's PTY allocation and default terminal attributes.
    if unsafe {
        libc::openpty(
            &mut master,
            &mut slave,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut size,
        )
    } < 0
    {
        return Err(io::Error::last_os_error());
    }
    // Successful openpty transfers two distinct descriptors to us.
    let master = unsafe { File::from_raw_fd(master) };
    let slave = unsafe { File::from_raw_fd(slave) };
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    {
        let mut attributes = MaybeUninit::<libc::termios>::uninit();
        if unsafe { libc::tcgetattr(master.as_raw_fd(), attributes.as_mut_ptr()) } < 0 {
            return Err(io::Error::last_os_error());
        }
        let mut attributes = unsafe { attributes.assume_init() };
        // Match Alacritty: canonical erase must remove a UTF-8 character, not just its last byte.
        attributes.c_iflag |= libc::IUTF8;
        if unsafe { libc::tcsetattr(master.as_raw_fd(), libc::TCSANOW, &attributes) } < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    for descriptor in [master.as_raw_fd(), slave.as_raw_fd()] {
        let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
        if flags < 0
            || unsafe { libc::fcntl(descriptor, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0
        {
            return Err(io::Error::last_os_error());
        }
    }
    let flags = unsafe { libc::fcntl(master.as_raw_fd(), libc::F_GETFL) };
    if flags < 0
        || unsafe { libc::fcntl(master.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
    {
        return Err(io::Error::last_os_error());
    }

    let command = &mut options.command;
    command
        .env("ALACRITTY_WINDOW_ID", window_id.to_string())
        .env("WINDOWID", window_id.to_string())
        .env_remove("XDG_ACTIVATION_TOKEN")
        .env_remove("DESKTOP_STARTUP_ID")
        .stdin(slave.try_clone()?)
        .stdout(slave.try_clone()?)
        .stderr(slave);
    let mask = options.child_signal_mask;
    // Only async-signal-safe operations run in the child. Command already mapped the slave to fd 0.
    unsafe {
        command.pre_exec(move || {
            if libc::setsid() < 0 || libc::ioctl(0, libc::TIOCSCTTY as _, 0) < 0 {
                return Err(io::Error::last_os_error());
            }
            if let Some(mask) = mask {
                mask.apply()?;
            }
            for signal in [
                libc::SIGCHLD,
                libc::SIGHUP,
                libc::SIGINT,
                libc::SIGQUIT,
                libc::SIGTERM,
                libc::SIGALRM,
            ] {
                if libc::signal(signal, libc::SIG_DFL) == libc::SIG_ERR {
                    return Err(io::Error::last_os_error());
                }
            }
            Ok(())
        });
    }

    // Register before spawn so a short-lived task cannot exit before we start observing SIGCHLD.
    let (sender, receiver) = UnixStream::pair()?;
    receiver.set_nonblocking(true)?;
    let signals = ChildSignal {
        receiver,
        registration: pipe::register(libc::SIGCHLD, sender)?,
    };
    let child = command.spawn()?;
    Ok(Pty {
        file: ManuallyDrop::new(master),
        child,
        signals,
        exited: false,
    })
}

impl EventedReadWrite for Pty {
    type Reader = File;
    type Writer = File;

    unsafe fn register(
        &mut self,
        poll: &Arc<Poller>,
        mut interest: Event,
        mode: PollMode,
    ) -> io::Result<()> {
        interest.key = READ_WRITE_TOKEN;
        unsafe {
            poll.add_with_mode(&*self.file, interest, mode)?;
        }
        if let Err(error) = unsafe {
            poll.add_with_mode(
                &self.signals.receiver,
                Event::readable(CHILD_EVENT_TOKEN),
                PollMode::Level,
            )
        } {
            if let Err(cleanup_error) = poll.delete(&*self.file) {
                log::error!("Failed to unregister OHOS PTY: {cleanup_error}");
            }
            return Err(error);
        }
        Ok(())
    }

    fn reregister(
        &mut self,
        poll: &Arc<Poller>,
        mut interest: Event,
        mode: PollMode,
    ) -> io::Result<()> {
        interest.key = READ_WRITE_TOKEN;
        poll.modify_with_mode(&*self.file, interest, mode)?;
        poll.modify_with_mode(
            &self.signals.receiver,
            Event::readable(CHILD_EVENT_TOKEN),
            PollMode::Level,
        )
    }

    fn deregister(&mut self, poll: &Arc<Poller>) -> io::Result<()> {
        let file_result = poll.delete(&*self.file);
        let signal_result = poll.delete(&self.signals.receiver);
        if let Err(error) = file_result {
            if let Err(signal_error) = signal_result {
                log::error!("Failed to unregister OHOS child signal: {signal_error}");
            }
            return Err(error);
        }
        signal_result
    }

    fn reader(&mut self) -> &mut File {
        &mut self.file
    }
    fn writer(&mut self) -> &mut File {
        &mut self.file
    }
}

impl EventedPty for Pty {
    fn next_child_event(&mut self) -> Option<ChildEvent> {
        let mut buffer = [0; 256];
        loop {
            match self.signals.receiver.read(&mut buffer) {
                Ok(0) => break,
                Ok(_) => {}
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) => {
                    log::error!("Failed to read OHOS child signal: {error}");
                    break;
                }
            }
        }
        if self.exited {
            return None;
        }
        match self.child.try_wait() {
            Ok(Some(status)) => {
                self.exited = true;
                Some(ChildEvent::Exited(Some(status)))
            }
            Ok(None) => None,
            Err(error) => {
                log::error!("Failed to obtain OHOS terminal exit status: {error}");
                self.exited = true;
                Some(ChildEvent::Exited(None))
            }
        }
    }
}

impl OnResize for Pty {
    fn on_resize(&mut self, size: WindowSize) {
        if size.num_cols == 0 || size.num_lines == 0 {
            return;
        }
        let size = window_size(size);
        if unsafe { libc::ioctl(self.file.as_raw_fd(), libc::TIOCSWINSZ, &size) } < 0 {
            log::error!("Failed to resize OHOS PTY: {}", io::Error::last_os_error());
        }
    }
}

impl Drop for Pty {
    fn drop(&mut self) {
        let group = unsafe { libc::tcgetpgrp(self.file.as_raw_fd()) };
        // Close before waitpid: a child can be stuck in terminal teardown while output is unread.
        // This is the sole drop of the master, and no subsequent code accesses it.
        unsafe {
            ManuallyDrop::drop(&mut self.file);
        }
        match self.child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => {}
            Err(error) => {
                log::error!("Failed to check OHOS terminal process: {error}");
                return;
            }
        }
        if group > 0 && unsafe { libc::killpg(group, libc::SIGHUP) } < 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::ESRCH) {
                log::error!("Failed to hang up OHOS terminal process group: {error}");
            }
        }
        // A shell may ignore SIGHUP or be blocked writing output. Do not leave it behind or block shutdown.
        if let Err(error) = self.child.kill() {
            if error.kind() != io::ErrorKind::InvalidInput {
                log::error!("Failed to kill OHOS terminal process: {error}");
            }
        }
        if let Err(error) = self.child.wait() {
            log::error!("Failed to reap OHOS terminal process: {error}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use alacritty_terminal::{
        event::{Event as TerminalEvent, EventListener},
        event_loop::{EventLoop, Msg},
        grid::Dimensions,
        sync::FairMutex,
        term::{Config, Term},
    };
    use std::{
        io::Write,
        sync::mpsc,
        time::{Duration, Instant},
    };

    type TestResult<T = ()> = Result<T, Box<dyn std::error::Error>>;

    fn options() -> io::Result<Options> {
        let mut command = Command::new(if cfg!(target_env = "ohos") {
            "/system/bin/sh"
        } else {
            "/bin/sh"
        });
        command
            .arg("-i")
            .env_clear()
            .env("PATH", "/bin:/usr/bin:/system/bin")
            .env("TERM", "xterm-256color");
        Ok(Options {
            command,
            child_signal_mask: Some(SignalMask::current()?),
            drain_on_exit: true,
        })
    }

    fn size() -> WindowSize {
        WindowSize {
            num_lines: 24,
            num_cols: 80,
            cell_width: 8,
            cell_height: 16,
        }
    }

    fn read_until(pty: &mut Pty, needle: &str) -> io::Result<String> {
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut output = Vec::new();
        while !String::from_utf8_lossy(&output).contains(needle) {
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    format!(
                        "waiting for {needle:?}: {}",
                        String::from_utf8_lossy(&output)
                    ),
                ));
            }
            let mut buffer = [0; 8192];
            match pty.reader().read(&mut buffer) {
                Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "PTY closed")),
                Ok(length) => output.extend_from_slice(&buffer[..length]),
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) =>
                {
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(error) => return Err(error),
            }
        }
        Ok(String::from_utf8_lossy(&output).into_owned())
    }

    #[test]
    fn real_tty_environment_directory_and_pipeline() -> TestResult {
        let directory = tempfile::tempdir()?;
        let directory_path = directory.path().canonicalize()?;
        let mut options = options()?;
        options
            .command
            .current_dir(&directory_path)
            .env("HOME", &directory_path)
            .env("ZED_PTY_TEST", "value with spaces");
        let mut pty = new(options, size(), 42)?;
        assert_ne!(
            unsafe { libc::fcntl(pty.file().as_raw_fd(), libc::F_GETFD) } & libc::FD_CLOEXEC,
            0
        );
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        {
            let mut attributes = MaybeUninit::<libc::termios>::uninit();
            if unsafe { libc::tcgetattr(pty.file().as_raw_fd(), attributes.as_mut_ptr()) } < 0 {
                return Err(io::Error::last_os_error().into());
            }
            assert_ne!(unsafe { attributes.assume_init() }.c_iflag & libc::IUTF8, 0);
        }
        pty.writer().write_all(b"test -t 0 && test -t 1 && test -t 2 && test \"$PWD\" = \"$HOME\" && test \"$ZED_PTY_TEST\" = 'value with spaces' && printf 'TTY_%s\\n' READY | cat\n")?;
        read_until(&mut pty, "TTY_READY")?;
        Ok(())
    }

    #[test]
    fn resize_interrupt_and_exit_status() -> TestResult {
        let options = options()?;
        let mut pty = std::thread::spawn(move || -> io::Result<Pty> {
            let mut blocked = MaybeUninit::<libc::sigset_t>::zeroed();
            if unsafe { libc::sigemptyset(blocked.as_mut_ptr()) } != 0 {
                return Err(io::Error::last_os_error());
            }
            let mut blocked = unsafe { blocked.assume_init() };
            if unsafe { libc::sigaddset(&mut blocked, libc::SIGINT) } != 0 {
                return Err(io::Error::last_os_error());
            }
            let result =
                unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &blocked, std::ptr::null_mut()) };
            if result != 0 {
                return Err(io::Error::from_raw_os_error(result));
            }
            new(options, size(), 1)
        })
        .join()
        .map_err(|_| io::Error::other("PTY spawn thread panicked"))??;
        pty.on_resize(WindowSize {
            num_lines: 37,
            num_cols: 113,
            ..size()
        });
        pty.writer()
            .write_all(b"stty size; printf 'SIZE_%s\\n' DONE\n")?;
        let output = read_until(&mut pty, "SIZE_DONE")?;
        assert!(output.contains("37 113"), "{output}");
        pty.writer().write_all(b"sleep 30\n")?;
        std::thread::sleep(Duration::from_millis(100));
        pty.writer()
            .write_all(b"\x03printf 'AFTER_%s\\n' INTERRUPT\n")?;
        read_until(&mut pty, "AFTER_INTERRUPT")?;
        pty.writer().write_all(b"exit 7\n")?;
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let mut output = [0; 1024];
            match pty.reader().read(&mut output) {
                Ok(_) => {}
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) || error.raw_os_error() == Some(libc::EIO) => {}
                Err(error) => return Err(error.into()),
            }
            if let Some(ChildEvent::Exited(Some(status))) = pty.next_child_event() {
                assert_eq!(status.code(), Some(7));
                break;
            }
            assert!(Instant::now() < deadline, "child did not exit");
            std::thread::sleep(Duration::from_millis(5));
        }
        Ok(())
    }

    #[test]
    fn dropping_blocked_output_reaps_child() -> TestResult {
        let mut options = options()?;
        options.command.args(["-c", "trap '' HUP; printf 'BLOCKED_%s\\n' READY; while :; do printf 'output output output\\n'; done"]);
        let mut pty = new(options, size(), 1)?;
        let child_id = i32::try_from(pty.child().id())?;
        read_until(&mut pty, "BLOCKED_READY")?;
        std::thread::sleep(Duration::from_millis(100));
        let start = Instant::now();
        drop(pty);
        assert!(start.elapsed() < Duration::from_secs(2));
        assert_eq!(
            unsafe { libc::waitpid(child_id, std::ptr::null_mut(), libc::WNOHANG) },
            -1
        );
        assert_eq!(
            io::Error::last_os_error().raw_os_error(),
            Some(libc::ECHILD)
        );
        Ok(())
    }

    #[test]
    fn invalid_command_and_directory_report_errors() -> TestResult {
        let mut command_options = options()?;
        command_options.command = Command::new("/nonexistent-zed-shell");
        assert!(
            matches!(new(command_options, size(), 1), Err(error) if error.kind() == io::ErrorKind::NotFound)
        );
        let mut directory_options = options()?;
        directory_options
            .command
            .current_dir("/nonexistent-zed-directory");
        assert!(
            matches!(new(directory_options, size(), 1), Err(error) if error.kind() == io::ErrorKind::NotFound)
        );
        Ok(())
    }

    #[derive(Clone)]
    struct Listener(mpsc::Sender<TerminalEvent>);

    impl EventListener for Listener {
        fn send_event(&self, event: TerminalEvent) {
            if let Err(error) = self.0.send(event) {
                log::debug!("test listener closed: {error}");
            }
        }
    }

    struct GridSize;
    impl Dimensions for GridSize {
        fn total_lines(&self) -> usize {
            24
        }
        fn screen_lines(&self) -> usize {
            24
        }
        fn columns(&self) -> usize {
            80
        }
    }

    #[test]
    fn alacritty_loop_receives_output_and_exit() -> TestResult {
        let options = options()?;
        let drain_on_exit = options.drain_on_exit;
        let pty = new(options, size(), 1)?;
        let (sender, receiver) = mpsc::channel();
        let listener = Listener(sender);
        let term = Arc::new(FairMutex::new(Term::new(
            Config::default(),
            &GridSize,
            listener.clone(),
        )));
        let event_loop = EventLoop::new(term, listener, pty, drain_on_exit, false)?;
        let channel = event_loop.channel();
        let worker = event_loop.spawn();
        channel.send(Msg::Input(
            b"printf '\\033]0;zed-pty-integration\\007'; exit 7\n"
                .as_slice()
                .into(),
        ))?;
        let mut title_received = false;
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            match receiver.recv_timeout(deadline.saturating_duration_since(Instant::now()))? {
                TerminalEvent::Title(title) if title == "zed-pty-integration" => {
                    title_received = true
                }
                TerminalEvent::ChildExit(status) => {
                    assert_eq!(status.code(), Some(7));
                    break;
                }
                _ => {}
            }
        }
        assert!(title_received, "VT output was not processed before exit");
        drop(
            worker
                .join()
                .map_err(|_| io::Error::other("PTY worker panicked"))?,
        );
        Ok(())
    }
}
