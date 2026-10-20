use anyhow::{Context as _, Result, anyhow};
use collections::HashMap;
use gpui::{App, AppContext as _, BorrowAppContext as _, Global, TaskExt as _};
use parking_lot::Mutex;
use rodio::{Decoder, Source, source::Buffered};
use settings::Settings;
use std::{io::Cursor, sync::Arc};
use util::ResultExt;

use crate::{AudioSettings, DeviceId, Sound};

#[path = "audio_pipeline/rodio_ext.rs"]
mod rodio_ext;
pub use rodio_ext::RodioExt;
mod input;
mod output;
pub use input::{InputStream, set_input_permission_handler};
pub use output::OutputStream as TestOutput;

#[derive(Default)]
pub struct Audio {
    output: Arc<Mutex<Option<TestOutput>>>,
    source_cache: HashMap<Sound, Buffered<Decoder<Cursor<Vec<u8>>>>>,
}

impl Global for Audio {}

impl Audio {
    pub fn play_sound(sound: Sound, cx: &mut App) {
        let device_id = AudioSettings::get_global(cx).output_audio_device.clone();
        cx.update_default_global(|this: &mut Self, cx| {
            let source = this.sound_source(sound, cx).log_err()?;
            let output = this.output.clone();
            cx.background_spawn(async move {
                let mut output = output.lock();
                if output
                    .as_ref()
                    .is_some_and(|output| output.check_error().is_err())
                {
                    if let Some(output) = output.take() {
                        output.check_error().log_err();
                    }
                }
                if output.is_none() {
                    *output = Some(open_test_output(device_id)?);
                }
                let output = output
                    .as_ref()
                    .context("Audio output was not initialized")?;
                output.mixer().add(source);
                anyhow::Ok(())
            })
            .detach_and_log_err(cx);
            Some(())
        });
    }

    pub fn end_call(cx: &mut App) {
        cx.update_default_global(|this: &mut Self, cx| {
            let output = std::mem::take(&mut this.output);
            cx.background_spawn(async move { drop(output) }).detach();
        });
    }

    fn sound_source(&mut self, sound: Sound, cx: &App) -> Result<impl Source + use<>> {
        if let Some(source) = self.source_cache.get(&sound) {
            return Ok(source.clone());
        }
        let path = format!("sounds/{}.wav", sound.file());
        let bytes = cx
            .asset_source()
            .load(&path)?
            .with_context(|| format!("No asset available for path {path}"))?
            .into_owned();
        let source = Decoder::new(Cursor::new(bytes))?.buffered();
        self.source_cache.insert(sound, source.clone());
        Ok(source)
    }
}

pub fn init(_cx: &mut App) {}

pub fn ensure_devices_initialized(cx: &mut App) {
    if cx.has_global::<AvailableAudioDevices>() {
        return;
    }
    cx.set_global(AvailableAudioDevices::default());
    let devices = cx.background_spawn(async move { output::available_devices() });
    cx.spawn(async move |cx| {
        if let Some(devices) = devices.await.log_err() {
            cx.update(|cx| cx.set_global(AvailableAudioDevices(devices)));
            cx.refresh();
        }
    })
    .detach();
}

pub fn resolve_device(_device_id: Option<&DeviceId>, _input: bool) -> Result<Device> {
    Err(anyhow!("audio devices are not available on OHOS yet"))
}

pub fn open_input_stream(device_id: Option<DeviceId>) -> Result<InputStream> {
    InputStream::open(device_id.as_deref())
}

pub fn open_test_output(device_id: Option<DeviceId>) -> Result<TestOutput> {
    TestOutput::open(device_id.as_deref())
}

pub struct Device;

#[derive(Clone, Debug)]
pub struct AudioDeviceInfo {
    pub id: DeviceId,
    pub desc: DeviceDescription,
}

impl AudioDeviceInfo {
    pub fn matches_input(&self, is_input: bool) -> bool {
        if is_input {
            self.desc.supports_input()
        } else {
            self.desc.supports_output()
        }
    }

    pub fn matches(&self, id: &DeviceId, is_input: bool) -> bool {
        &self.id == id && self.matches_input(is_input)
    }
}

impl std::fmt::Display for AudioDeviceInfo {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.desc.name.fmt(f)
    }
}

#[derive(Default, Clone, Debug)]
pub struct AvailableAudioDevices(pub Vec<AudioDeviceInfo>);

impl Global for AvailableAudioDevices {}

#[derive(Clone, Debug)]
pub struct DeviceDescription {
    pub name: String,
    pub input: bool,
    pub output: bool,
}

impl DeviceDescription {
    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn supports_input(&self) -> bool {
        self.input
    }

    pub fn supports_output(&self) -> bool {
        self.output
    }
}
