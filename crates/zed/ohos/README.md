# Zed OHOS integration

The OpenHarmony application project lives in the sibling `zed-ohos` directory. It supplies the Ability entry point and the external `gpui-ohos` platform.

On OHOS, this crate exposes `run_with_ohos_platform(platform, base_path)`. The function stores Zed's data under the Ability's application directory, starts the normal Zed initialization, and retains the embedded GPUI application handle for the lifetime of the Ability process.
