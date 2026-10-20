use anyhow::{Context as _, Result};
use crossbeam_queue::ArrayQueue;
use ohos_audio_sys as native;
use parking_lot::RwLock;
use std::{
    ffi::c_void,
    ptr::{self, NonNull},
    sync::{
        Arc,
        atomic::{AtomicU32, Ordering},
    },
};

use super::output::{Builder, check, select_input_device};
use crate::SAMPLE_RATE;

type PermissionHandler = Arc<dyn Fn() -> Result<()> + Send + Sync>;
static PERMISSION_HANDLER: RwLock<Option<PermissionHandler>> = RwLock::new(None);

pub fn set_input_permission_handler(handler: impl Fn() -> Result<()> + Send + Sync + 'static) {
    *PERMISSION_HANDLER.write() = Some(Arc::new(handler));
}

struct CallbackState {
    samples: ArrayQueue<f32>,
    error: AtomicU32,
}

pub struct InputStream {
    capturer: NonNull<native::OH_AudioCapturer>,
    callback: Option<Box<CallbackState>>,
    reported_error: bool,
}

// Native callbacks use only the synchronized state, whose allocation is retained
// until Release has joined the callbacks. Stream operations have one owner.
unsafe impl Send for InputStream {}

impl InputStream {
    pub fn open(device_id: Option<&str>) -> Result<Self> {
        // Permission UI runs on the Ability thread while the audio worker waits.
        // Never hold the handler lock across that asynchronous bridge request.
        let request_permission = PERMISSION_HANDLER
            .read()
            .clone()
            .context("Microphone permission handler is not installed")?;
        request_permission()?;

        let builder = Builder::new(native::OH_AudioStream_Type_AUDIOSTREAM_TYPE_CAPTURER)?;
        let mut callback = Box::new(CallbackState {
            samples: ArrayQueue::new(SAMPLE_RATE.get() as usize / 5),
            error: AtomicU32::new(0),
        });
        let user_data = ptr::from_mut(callback.as_mut()).cast::<c_void>();
        let mut capturer = ptr::null_mut();
        unsafe {
            check(
                native::OH_AudioStreamBuilder_SetSamplingRate(
                    builder.0.as_ptr(),
                    SAMPLE_RATE.get() as i32,
                ),
                "set input sample rate",
            )?;
            check(
                native::OH_AudioStreamBuilder_SetChannelCount(builder.0.as_ptr(), 1),
                "set input channel count",
            )?;
            check(
                native::OH_AudioStreamBuilder_SetSampleFormat(
                    builder.0.as_ptr(),
                    native::OH_AudioStream_SampleFormat_AUDIOSTREAM_SAMPLE_S16LE,
                ),
                "set input sample format",
            )?;
            check(
                native::OH_AudioStreamBuilder_SetCapturerInfo(
                    builder.0.as_ptr(),
                    native::OH_AudioStream_SourceType_AUDIOSTREAM_SOURCE_TYPE_MIC,
                ),
                "set microphone source",
            )?;
            check(
                native::OH_AudioStreamBuilder_SetCapturerCallback(
                    builder.0.as_ptr(),
                    native::OH_AudioCapturer_Callbacks {
                        OH_AudioCapturer_OnReadData: Some(read_data),
                        OH_AudioCapturer_OnStreamEvent: None,
                        OH_AudioCapturer_OnInterruptEvent: Some(on_interrupt),
                        OH_AudioCapturer_OnError: Some(on_error),
                    },
                    user_data,
                ),
                "set input callbacks",
            )?;
            check(
                native::OH_AudioStreamBuilder_GenerateCapturer(builder.0.as_ptr(), &mut capturer),
                "create microphone input",
            )?;
        }
        let stream = Self {
            capturer: NonNull::new(capturer).context("OHAudio returned a null capturer")?,
            callback: Some(callback),
            reported_error: false,
        };
        if let Some(device_id) = device_id {
            select_input_device(stream.capturer, device_id)?;
        }
        check(
            unsafe { native::OH_AudioCapturer_Start(stream.capturer.as_ptr()) },
            "start microphone input",
        )?;
        log::info!("Opened OHOS microphone: {} Hz, 1 channel", SAMPLE_RATE);
        Ok(stream)
    }
}

impl Iterator for InputStream {
    type Item = rodio::Sample;

    fn next(&mut self) -> Option<Self::Item> {
        let callback = self.callback.as_ref()?;
        if let Err(error) = check(
            callback.error.load(Ordering::Relaxed),
            "capture microphone audio",
        ) {
            if !self.reported_error {
                log::error!("{error:#}");
                self.reported_error = true;
            }
            return None;
        }
        // This iterator is consumed by the output's realtime callback. Waiting
        // here would stall output and could deadlock stream shutdown.
        Some(callback.samples.pop().unwrap_or(0.0))
    }
}

impl rodio::Source for InputStream {
    fn current_span_len(&self) -> Option<usize> {
        None
    }
    fn channels(&self) -> rodio::ChannelCount {
        rodio::nz!(1)
    }
    fn sample_rate(&self) -> rodio::SampleRate {
        SAMPLE_RATE
    }
    fn total_duration(&self) -> Option<std::time::Duration> {
        None
    }
}

impl Drop for InputStream {
    fn drop(&mut self) {
        if let Err(error) = check(
            unsafe { native::OH_AudioCapturer_Release(self.capturer.as_ptr()) },
            "release microphone input",
        ) {
            log::error!("{error:#}");
            // Failed release does not establish that callbacks have stopped.
            if let Some(callback) = self.callback.take() {
                Box::leak(callback);
            }
        }
    }
}

unsafe extern "C" fn read_data(
    _capturer: *mut native::OH_AudioCapturer,
    user_data: *mut c_void,
    buffer: *mut c_void,
    length: i32,
) -> i32 {
    if user_data.is_null() || buffer.is_null() || length <= 0 || length % 2 != 0 {
        return -1;
    }
    let callback = unsafe { &*user_data.cast::<CallbackState>() };
    let buffer = unsafe { std::slice::from_raw_parts(buffer.cast::<u8>(), length as usize) };
    for bytes in buffer.chunks_exact(2) {
        let sample = i16::from_le_bytes([bytes[0], bytes[1]]) as f32 / 32768.0;
        // Bound latency when the consumer is paused, without allocating or
        // blocking the platform's capture thread.
        callback.samples.force_push(sample);
    }
    0
}

unsafe extern "C" fn on_error(
    _capturer: *mut native::OH_AudioCapturer,
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
    capturer: *mut native::OH_AudioCapturer,
    user_data: *mut c_void,
    _force: native::OH_AudioInterrupt_ForceType,
    hint: native::OH_AudioInterrupt_Hint,
) -> i32 {
    if hint == native::OH_AudioInterrupt_Hint_AUDIOSTREAM_INTERRUPT_HINT_PAUSE
        || hint == native::OH_AudioInterrupt_Hint_AUDIOSTREAM_INTERRUPT_HINT_STOP
    {
        unsafe {
            on_error(
                capturer,
                user_data,
                native::OH_AudioStream_Result_AUDIOSTREAM_ERROR_ILLEGAL_STATE,
            )
        };
    }
    0
}
