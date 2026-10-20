use std::{
    io::{self, Write},
    os::unix::net::UnixStream,
    process::ExitCode,
};

fn run() -> io::Result<()> {
    let socket = std::env::var_os("ZED_ASKPASS_SOCKET")
        .filter(|path| !path.is_empty())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing askpass socket"))?;
    let mut prompt = std::env::args()
        .skip(1)
        .collect::<Vec<_>>()
        .join("\0")
        .into_bytes();
    if prompt.last() != Some(&0) {
        prompt.push(0);
    }
    let mut stream = UnixStream::connect(socket)?;
    stream.write_all(&prompt)?;
    io::copy(&mut stream, &mut io::stdout())?;
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("Zed authentication helper failed: {error}");
            ExitCode::FAILURE
        }
    }
}
