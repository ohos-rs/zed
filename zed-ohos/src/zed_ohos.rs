use log::{Level, LevelFilter, Log, Metadata, Record};
use openharmony_ability::OpenHarmonyApp;
use openharmony_ability_plugin_permission::{PermissionBridgePlugin, PermissionExt};
use std::ffi::{CString, c_char};

struct ZedHiLog;

static LOGGER: ZedHiLog = ZedHiLog;

#[link(name = "hilog_ndk.z")]
unsafe extern "C" {
    fn OH_LOG_Print(
        kind: u32,
        level: u32,
        domain: u32,
        tag: *const c_char,
        format: *const c_char,
        ...
    ) -> i32;
}

impl Log for ZedHiLog {
    fn enabled(&self, metadata: &Metadata<'_>) -> bool {
        metadata.level() <= log::max_level()
    }

    fn log(&self, record: &Record<'_>) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let level = match record.level() {
            Level::Error => 6,
            Level::Warn => 5,
            Level::Info => 4,
            Level::Debug | Level::Trace => 3,
        };
        let message = format!("{}: {}", record.target(), record.args()).replace('\0', "\\0");
        let mut remaining = message.as_str();
        while !remaining.is_empty() {
            let length = remaining.floor_char_boundary(3000);
            let (chunk, rest) = remaining.split_at(length);
            match CString::new(chunk) {
                Ok(chunk) => {
                    // The binding's log adapter passes unterminated targets and uses the message
                    // as a printf format. Keep both C strings alive and pass text as an argument.
                    let result = unsafe {
                        OH_LOG_Print(
                            0,
                            level,
                            0,
                            c"zed".as_ptr(),
                            c"%{public}s".as_ptr(),
                            chunk.as_ptr(),
                        )
                    };
                    if result < 0 {
                        eprintln!("HiLog rejected a Zed log record: {result}");
                    }
                }
                Err(error) => eprintln!("Failed to encode a Zed log record: {error}"),
            }
            remaining = rest;
        }
    }

    fn flush(&self) {}
}

#[openharmony_ability_derive::ability]
fn openharmony_app(app: OpenHarmonyApp) {
    match log::set_logger(&LOGGER) {
        Ok(()) => log::set_max_level(LevelFilter::Info),
        Err(error) => log::warn!("Failed to install the Zed OHOS logger: {error}"),
    }
    if let Err(error) = app.register_plugin(PermissionBridgePlugin) {
        log::error!("Failed to register the OpenHarmony permission bridge: {error}");
        return;
    }
    audio::set_input_permission_handler({
        let app = app.clone();
        move || {
            let responses =
                futures::executor::block_on(app.request_permission("ohos.permission.MICROPHONE"))?;
            anyhow::ensure!(
                responses.len() == 1
                    && responses.first().is_some_and(|response| response.code == 0),
                "Microphone access was denied. Allow microphone access for Zed in system settings."
            );
            Ok(())
        }
    });
    let base_path = app.base_path();
    let platform = match gpui_ohos::try_current_platform(
        app,
        gpui_ohos::PlatformOptions {
            native_menus: true,
            persist_file_access: true,
        },
    ) {
        Ok(platform) => platform,
        Err(error) => {
            log::error!("Failed to initialize the Zed OHOS platform: {error:#}");
            return;
        }
    };
    zed_core::run_with_ohos_platform(platform, base_path.as_deref());
}
