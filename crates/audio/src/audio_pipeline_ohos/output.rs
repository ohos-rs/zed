use anyhow::{Context as _, Result, bail, ensure};
use ohos_audio_sys as native;
use parking_lot::Mutex;
use rodio::mixer::{Mixer, MixerSource};
use std::{
    ffi::{CStr, c_void},
    panic::{AssertUnwindSafe, catch_unwind},
    ptr::{self, NonNull},
    sync::atomic::{AtomicU32, Ordering},
};

use super::{AudioDeviceInfo, DeviceDescription};
use crate::{CHANNEL_COUNT, SAMPLE_RATE};

struct CallbackState {
    source: Mutex<MixerSource>,
    error: AtomicU32,
}

pub struct OutputStream {
    renderer: NonNull<native::OH_AudioRenderer>,
    callback: Option<Box<CallbackState>>,
    mixer: Mixer,
}

// The owner serializes stream operations. Native callbacks only access the boxed,
// synchronized state, which stays alive until Release has joined those callbacks.
unsafe impl Send for OutputStream {}

impl OutputStream {
    pub fn open(device_id: Option<&str>) -> Result<Self> {
        let builder = Builder::new(native::OH_AudioStream_Type_AUDIOSTREAM_TYPE_RENDERER)?;
        let (mixer, source) = rodio::mixer::mixer(CHANNEL_COUNT, SAMPLE_RATE);
        mixer.add(rodio::source::Zero::new(CHANNEL_COUNT, SAMPLE_RATE));
        let mut callback = Box::new(CallbackState {
            source: Mutex::new(source),
            error: AtomicU32::new(0),
        });
        let user_data = ptr::from_mut(callback.as_mut()).cast::<c_void>();
        let mut renderer = ptr::null_mut();
        unsafe {
            check(
                native::OH_AudioStreamBuilder_SetSamplingRate(
                    builder.0.as_ptr(),
                    SAMPLE_RATE.get() as i32,
                ),
                "set output sample rate",
            )?;
            check(
                native::OH_AudioStreamBuilder_SetChannelCount(
                    builder.0.as_ptr(),
                    CHANNEL_COUNT.get() as i32,
                ),
                "set output channel count",
            )?;
            check(
                native::OH_AudioStreamBuilder_SetSampleFormat(
                    builder.0.as_ptr(),
                    native::OH_AudioStream_SampleFormat_AUDIOSTREAM_SAMPLE_S16LE,
                ),
                "set output sample format",
            )?;
            check(
                native::OH_AudioStreamBuilder_SetRendererInfo(
                    builder.0.as_ptr(),
                    native::OH_AudioStream_Usage_AUDIOSTREAM_USAGE_MUSIC,
                ),
                "set output usage",
            )?;
            check(
                native::OH_AudioStreamBuilder_SetRendererCallback(
                    builder.0.as_ptr(),
                    native::OH_AudioRenderer_Callbacks {
                        OH_AudioRenderer_OnWriteData: Some(write_data),
                        OH_AudioRenderer_OnStreamEvent: None,
                        OH_AudioRenderer_OnInterruptEvent: Some(on_interrupt),
                        OH_AudioRenderer_OnError: Some(on_error),
                    },
                    user_data,
                ),
                "set output callbacks",
            )?;
            check(
                native::OH_AudioStreamBuilder_GenerateRenderer(builder.0.as_ptr(), &mut renderer),
                "create audio output",
            )?;
        }
        let output = Self {
            renderer: NonNull::new(renderer).context("OHAudio returned a null renderer")?,
            callback: Some(callback),
            mixer,
        };
        if let Some(device_id) = device_id {
            output.select_device(device_id)?;
        }
        check(
            unsafe { native::OH_AudioRenderer_Start(output.renderer.as_ptr()) },
            "start audio output",
        )?;
        log::info!(
            "Opened OHOS audio output: {} Hz, {} channels",
            SAMPLE_RATE,
            CHANNEL_COUNT
        );
        Ok(output)
    }

    pub fn mixer(&self) -> &Mixer {
        &self.mixer
    }

    pub fn check_error(&self) -> Result<()> {
        if let Some(callback) = &self.callback {
            check(callback.error.load(Ordering::Relaxed), "render audio")?;
        }
        Ok(())
    }

    fn select_device(&self, device_id: &str) -> Result<()> {
        let devices = Devices::new(DeviceDirection::Output)?;
        for descriptor in devices.descriptors()? {
            if devices.info(*descriptor)?.id == device_id {
                if let Some(routing) = DeviceRouting::new()? {
                    routing.select(self.renderer, *descriptor)?;
                } else {
                    log::warn!("Audio device selection is unavailable; using the system default");
                }
                return Ok(());
            }
        }
        log::warn!(
            "Selected audio output device {device_id} is unavailable; using the system default"
        );
        Ok(())
    }
}

impl Drop for OutputStream {
    fn drop(&mut self) {
        if let Err(error) = check(
            unsafe { native::OH_AudioRenderer_Release(self.renderer.as_ptr()) },
            "release audio output",
        ) {
            log::error!("{error:#}");
            // A failed release does not guarantee that callbacks have stopped.
            // Retain their state rather than letting native code use freed memory.
            if let Some(callback) = self.callback.take() {
                Box::leak(callback);
            }
        }
    }
}

unsafe extern "C" fn write_data(
    _renderer: *mut native::OH_AudioRenderer,
    user_data: *mut c_void,
    buffer: *mut c_void,
    length: i32,
) -> i32 {
    if user_data.is_null() || buffer.is_null() || length <= 0 || length % 2 != 0 {
        return native::OH_AudioData_Callback_Result_AUDIO_DATA_CALLBACK_RESULT_INVALID;
    }
    // OHAudio owns the writable buffer for this callback. Bytes avoid assuming
    // the alignment of a platform-provided buffer when writing PCM samples.
    let state = unsafe { &*user_data.cast::<CallbackState>() };
    let buffer = unsafe { std::slice::from_raw_parts_mut(buffer.cast::<u8>(), length as usize) };
    let result = catch_unwind(AssertUnwindSafe(|| {
        if let Some(mut source) = state.source.try_lock() {
            for bytes in buffer.chunks_exact_mut(2) {
                let sample = source.next().unwrap_or(0.0);
                let sample = if sample.is_finite() {
                    sample.clamp(-1.0, 1.0)
                } else {
                    0.0
                };
                let sample = (sample * 32768.0) as i16;
                bytes.copy_from_slice(&sample.to_le_bytes());
            }
        } else {
            buffer.fill(0);
        }
    }));
    if result.is_err() {
        state.error.store(
            native::OH_AudioStream_Result_AUDIOSTREAM_ERROR_SYSTEM,
            Ordering::Relaxed,
        );
        buffer.fill(0);
        return native::OH_AudioData_Callback_Result_AUDIO_DATA_CALLBACK_RESULT_INVALID;
    }
    native::OH_AudioData_Callback_Result_AUDIO_DATA_CALLBACK_RESULT_VALID
}

unsafe extern "C" fn on_error(
    _renderer: *mut native::OH_AudioRenderer,
    user_data: *mut c_void,
    error: native::OH_AudioStream_Result,
) -> i32 {
    if !user_data.is_null() {
        unsafe { &*user_data.cast::<CallbackState>() }
            .error
            .store(error, Ordering::Relaxed);
    }
    0
}

unsafe extern "C" fn on_interrupt(
    renderer: *mut native::OH_AudioRenderer,
    user_data: *mut c_void,
    _force: native::OH_AudioInterrupt_ForceType,
    hint: native::OH_AudioInterrupt_Hint,
) -> i32 {
    if hint == native::OH_AudioInterrupt_Hint_AUDIOSTREAM_INTERRUPT_HINT_PAUSE
        || hint == native::OH_AudioInterrupt_Hint_AUDIOSTREAM_INTERRUPT_HINT_STOP
    {
        unsafe {
            on_error(
                renderer,
                user_data,
                native::OH_AudioStream_Result_AUDIOSTREAM_ERROR_ILLEGAL_STATE,
            )
        };
    }
    0
}

pub(super) struct Builder(pub(super) NonNull<native::OH_AudioStreamBuilder>);

impl Builder {
    pub(super) fn new(stream_type: native::OH_AudioStream_Type) -> Result<Self> {
        let mut builder = ptr::null_mut();
        check(
            unsafe { native::OH_AudioStreamBuilder_Create(&mut builder, stream_type) },
            "create audio builder",
        )?;
        Ok(Self(
            NonNull::new(builder).context("OHAudio returned a null builder")?,
        ))
    }
}

impl Drop for Builder {
    fn drop(&mut self) {
        if let Err(error) = check(
            unsafe { native::OH_AudioStreamBuilder_Destroy(self.0.as_ptr()) },
            "destroy audio builder",
        ) {
            log::error!("{error:#}");
        }
    }
}

struct Devices {
    manager: NonNull<native::OH_AudioRoutingManager>,
    devices: NonNull<native::OH_AudioDeviceDescriptorArray>,
    direction: DeviceDirection,
}

#[derive(Clone, Copy)]
enum DeviceDirection {
    Input,
    Output,
}

impl DeviceDirection {
    fn label(self) -> &'static str {
        match self {
            Self::Input => "input",
            Self::Output => "output",
        }
    }

    fn usage(self) -> native::OH_AudioDevice_Usage {
        match self {
            Self::Input => native::OH_AudioDevice_Usage_AUDIO_DEVICE_USAGE_MEDIA_INPUT,
            Self::Output => native::OH_AudioDevice_Usage_AUDIO_DEVICE_USAGE_MEDIA_OUTPUT,
        }
    }
}

impl Devices {
    fn new(direction: DeviceDirection) -> Result<Self> {
        let mut manager = ptr::null_mut();
        check(
            unsafe { native::OH_AudioManager_GetAudioRoutingManager(&mut manager) },
            "get audio routing manager",
        )?;
        let manager = NonNull::new(manager).context("OHAudio returned a null routing manager")?;
        let mut devices = ptr::null_mut();
        check(
            unsafe {
                native::OH_AudioRoutingManager_GetAvailableDevices(
                    manager.as_ptr(),
                    direction.usage(),
                    &mut devices,
                )
            },
            &format!("list audio {} devices", direction.label()),
        )?;
        Ok(Self {
            manager,
            devices: NonNull::new(devices).context("OHAudio returned a null device list")?,
            direction,
        })
    }

    fn descriptors(&self) -> Result<&[*mut native::OH_AudioDeviceDescriptor]> {
        let devices = unsafe { self.devices.as_ref() };
        if devices.size == 0 {
            return Ok(&[]);
        }
        ensure!(
            !devices.descriptors.is_null(),
            "OHAudio returned null device descriptors"
        );
        Ok(unsafe { std::slice::from_raw_parts(devices.descriptors, devices.size as usize) })
    }

    fn info(&self, descriptor: *mut native::OH_AudioDeviceDescriptor) -> Result<AudioDeviceInfo> {
        ensure!(
            !descriptor.is_null(),
            "OHAudio returned a null device descriptor"
        );
        let mut id = 0;
        let mut name = ptr::null_mut();
        check(
            unsafe { native::OH_AudioDeviceDescriptor_GetDeviceId(descriptor, &mut id) },
            "get audio device ID",
        )?;
        check(
            unsafe { native::OH_AudioDeviceDescriptor_GetDeviceName(descriptor, &mut name) },
            "get audio device name",
        )?;
        ensure!(!name.is_null(), "OHAudio returned a null device name");
        let name = unsafe { CStr::from_ptr(name) }
            .to_string_lossy()
            .into_owned();
        Ok(AudioDeviceInfo {
            id: format!("ohos-{}-{id}", self.direction.label()),
            desc: DeviceDescription {
                name: if name.is_empty() {
                    format!("Audio {} {id}", self.direction.label())
                } else {
                    name
                },
                input: matches!(self.direction, DeviceDirection::Input),
                output: matches!(self.direction, DeviceDirection::Output),
            },
        })
    }
}

impl Drop for Devices {
    fn drop(&mut self) {
        if let Err(error) = check(
            unsafe {
                native::OH_AudioRoutingManager_ReleaseDevices(
                    self.manager.as_ptr(),
                    self.devices.as_ptr(),
                )
            },
            "release audio devices",
        ) {
            log::error!("{error:#}");
        }
    }
}

struct DeviceRouting {
    library: libloading::Library,
    manager: NonNull<native::OH_AudioDeviceEnhanceManager>,
}

impl DeviceRouting {
    fn new() -> Result<Option<Self>> {
        // Per-stream device selection was added in API 26. Resolve these symbols
        // lazily so the application can still load and play on older systems.
        let library = unsafe { libloading::Library::new("libohaudio.so") }?;
        let manager = unsafe {
            let Ok(get_manager) = library.get::<unsafe extern "C" fn(
                *mut *mut native::OH_AudioDeviceEnhanceManager,
            ) -> u32>(
                b"OH_AudioManager_GetAudioDeviceEnhanceManager\0"
            ) else {
                return Ok(None);
            };
            let supported = library.get::<unsafe extern "C" fn(
                *mut native::OH_AudioDeviceEnhanceManager,
                *mut bool,
            ) -> u32>(
                b"OH_AudioDeviceEnhanceManager_IsEnhancedRoutingSupported\0",
            )?;
            let mut manager = ptr::null_mut();
            check(
                get_manager(&mut manager),
                "get audio device selection manager",
            )?;
            let manager = NonNull::new(manager)
                .context("OHAudio returned a null device selection manager")?;
            let mut is_supported = false;
            check(
                supported(manager.as_ptr(), &mut is_supported),
                "query audio device selection support",
            )?;
            if !is_supported {
                return Ok(None);
            }
            manager
        };
        Ok(Some(Self { library, manager }))
    }

    fn select(
        &self,
        renderer: NonNull<native::OH_AudioRenderer>,
        descriptor: *mut native::OH_AudioDeviceDescriptor,
    ) -> Result<()> {
        let select = unsafe {
            self.library.get::<unsafe extern "C" fn(
                *mut native::OH_AudioDeviceEnhanceManager,
                *mut native::OH_AudioRenderer,
                *mut native::OH_AudioDeviceDescriptor,
            ) -> u32>(
                b"OH_AudioDeviceEnhanceManager_SelectOutputDeviceForAudioRenderer\0"
            )
        }?;
        check(
            unsafe { select(self.manager.as_ptr(), renderer.as_ptr(), descriptor) },
            "select audio output device",
        )
    }

    fn select_input(
        &self,
        capturer: NonNull<native::OH_AudioCapturer>,
        descriptor: *mut native::OH_AudioDeviceDescriptor,
    ) -> Result<()> {
        let select = unsafe {
            self.library.get::<unsafe extern "C" fn(
                *mut native::OH_AudioDeviceEnhanceManager,
                *mut native::OH_AudioCapturer,
                *mut native::OH_AudioDeviceDescriptor,
            ) -> u32>(
                b"OH_AudioDeviceEnhanceManager_SelectInputDeviceForAudioCapturer\0"
            )
        }?;
        check(
            unsafe { select(self.manager.as_ptr(), capturer.as_ptr(), descriptor) },
            "select audio input device",
        )
    }
}

pub(super) fn select_input_device(
    capturer: NonNull<native::OH_AudioCapturer>,
    device_id: &str,
) -> Result<()> {
    let devices = Devices::new(DeviceDirection::Input)?;
    for descriptor in devices.descriptors()? {
        if devices.info(*descriptor)?.id == device_id {
            if let Some(routing) = DeviceRouting::new()? {
                routing.select_input(capturer, *descriptor)?;
            } else {
                log::warn!("Audio device selection is unavailable; using the system default");
            }
            return Ok(());
        }
    }
    log::warn!("Selected audio input device {device_id} is unavailable; using the system default");
    Ok(())
}

pub(super) fn available_devices() -> Result<Vec<AudioDeviceInfo>> {
    if DeviceRouting::new()?.is_none() {
        // Keep System Default as the only choice if this device cannot honor
        // per-stream routing; showing alternatives would silently ignore them.
        return Ok(Vec::new());
    }
    let mut available = Vec::new();
    for direction in [DeviceDirection::Output, DeviceDirection::Input] {
        let devices = Devices::new(direction)?;
        for descriptor in devices.descriptors()? {
            available.push(devices.info(*descriptor)?);
        }
    }
    Ok(available)
}

pub(super) fn check(result: u32, operation: &str) -> Result<()> {
    if result != 0 {
        bail!("Failed to {operation}: OHAudio error {result}");
    }
    Ok(())
}
