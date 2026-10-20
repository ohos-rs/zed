> [!IMPORTANT]
> Remove this line to confirm you've reviewed this PR before submitting.

# Zed for OpenHarmony

This independent Cargo project supplies the OpenHarmony Ability entry points,
ArkTS host, native library, packaging and bundled tools for the parent Zed
checkout. Both projects are published on `ohos-rs/zed:feat-zed-ohos`.

## Sources

Initialize the single runtime source tree from the repository root:

```sh
git submodule update --init --recursive
```

`../vendor/gpui-ohos` is pinned to `dc443d9f7f25ed7a344b9924492a41e1149bb677`.
Its nested Ability submodule is pinned to
`6454a9c34a62add21e258d717b09f12d142120b4`; that runtime pins IME binding
`d47a3d9f0661dbe73720240174c1f37eb8fc7858`.
Rust and ArkTS both consume this same Ability directory. Do not copy the runtime
into the application or substitute a registry package on only one side.
Zed's local GPUI crate supplies the native menu and RGBA capture interfaces and
retains the existing terminal interfaces for the separate terminal work.

The application remains a separate Cargo workspace. The parent workspace
excludes this project and the nested runtime workspaces so dependency metadata
is inherited from each package's actual workspace.

## Build

Use the installed DevEco SDK and the pinned Rust 1.98.1 toolchain:

```sh
cd zed-ohos
ohrs build --release --arch arm64
sdk_native="${OHOS_NDK_HOME:-/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native}"
mkdir -p ohos/entry/libs/arm64-v8a
"$sdk_native/llvm/bin/llvm-strip" --strip-unneeded \
  -o ohos/entry/libs/arm64-v8a/libzed.so dist/arm64-v8a/libzed.so
cp "$sdk_native/llvm/lib/aarch64-linux-ohos/libc++_shared.so" ohos/entry/libs/arm64-v8a/
cp dist/index.d.ts ohos/entry/src/main/ets/types/libzed/Index.d.ts
cd ohos
ohpm-rs install --all
hvigorw --mode module -p module=entry@default -p product=default \
  -p buildMode=debug assembleHap --no-daemon
```

`ohpm-rs` resolves the source packages' `workspace:*` dependencies. Use DevEco's
`hvigorw` and Java runtime when those commands are not on PATH. The scripts in
`script/` build and package the existing Node, Git, SSH and Rust tools.

## Windows, menus and lifecycle

`EntryAbility` loads Zed once. `WorkspaceAbility` hosts subsequent independent
workspace windows through the shared runtime. The system renders menus and
prompts, while Zed owns action validation, shortcuts, unsaved-change confirmation,
workspace persistence and the decision to exit after its last window closes.
The app disables the runtime's extra menu bar and shortcut dispatcher.

Zed retains its existing asynchronous save confirmation workflow. Cancelling or
failing a close leaves the window available for another request.
Native menu opening clears the current window's pending shortcut prefix even
though the editor's GPUI focus is retained.

Restart state must be persisted before requesting the SDK restart. A rejected
restart request retains existing windows and shows an error. An accepted restart
may terminate the process before shutdown observers finish.

## Verification limits

The integrated ARM64 Release library and ArkTS host were installed on a fresh
DevEco IDE MateBook Pro API 26 emulator. Native application menus and submenus,
settings, independent workspaces, focus return, unsaved-buffer restoration,
close cancellation, restart confirmation cancellation and accepted process
restart were exercised. The owned emulator was stopped and deleted afterward.

Host regressions and SDK compilation do not establish complete native parity.
Creation-time input routing, task-center close/cancel visibility, independent
main-window non-activating launch and hardware-enforced provider permissions
remain separate acceptance items. The SDK does not promise `focus=false` for an
independent UIAbility main window.
Normal exit also logged `timed out waiting on app_will_quit`; the buffer was
restored after relaunch, but complete shutdown-observer completion remains
unverified.

The existing terminal implementation and tool packaging are retained; this
integration does not add terminal fixes. Native validation uses a new dedicated
DevEco IDE emulator and closes only that owned instance afterward.
