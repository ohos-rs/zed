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

The local runtime integration uses GPUI OHOS PR #7 at
`bccbfe75dd578f8c6685ea77e75ccb70d85eb952` with Ability PR #109 at
`b22ac744017d86cb6e7acddfc0f230126c536bff`. That runtime pins native bindings
`43aec6c125503a47f7e875c793aaf2462e91ad19`.
Rust and ArkTS both consume this same Ability directory. Do not copy the runtime
into the application or substitute a registry package on only one side.
Zed's existing `Window::prompt` and `PlatformWindow::prompt` interfaces are used
without additional styling parameters or application-global prompt state.
The existing terminal changes remain separate from this integration.

The application remains a separate Cargo workspace. The parent workspace
excludes this project and the nested runtime workspaces so dependency metadata
is inherited from each package's actual workspace.

## Build

Use the installed DevEco API 26 SDK and the pinned Rust 1.98.1 toolchain.
The application enables the runtime's API-26 input consumption and declares
HarmonyOS 7.0 / API 26 as its minimum supported version:

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
Ability resolves native confirmation colors from the target window's system
resources, following its light or dark appearance. Zed themes keep their own
editor colors; no extra color protocol is added to GPUI's prompt interface.
Native color channels use the SDK's 0–255 alpha range directly.
Theme selection uses the system appearance captured before previewing, so native
chrome updates do not undo the selected theme.

Zed retains its existing asynchronous save confirmation workflow. Cancelling or
failing a close leaves the window available for another request.
Ability defers pre-IME keys until their disposition is known. Foreground wakes
can continue while waiting, so an early wake cannot suppress an unconsumed
shortcut. IME edits resolve their key before the committed text is delivered.

Native menu opening clears the current window's pending shortcut prefix even
though the editor's GPUI focus is retained. Ability renders confirmation cards
with neutral semantic colors, an outlined cancel control and one primary action,
using a wrapping desktop footer and a stacked layout in narrow windows.

Persistent file grants are explicitly enabled by the Zed host. The runtime
restores them in the background after the first presented frame; the HAP
declares `ohos.permission.FILE_ACCESS_PERSIST`.

Restart state must be persisted before requesting the SDK restart. A rejected
restart request retains existing windows and shows an error. An accepted restart
may terminate the process before shutdown observers finish.

Window teardown retains its native registration when destruction fails and
propagates the error. Late completion cannot unregister a replacement window
with the same ID. Managed floating windows use the same cleanup path as their
window manager. This preserves runtime ownership; it does not restore a GPUI
view which the application has already released.

The OHOS Cargo configuration uses Zed's existing external-package update
explanation. Automatic Linux binary replacement is disabled, and Check for
Updates directs users to install a newer HarmonyOS package. An automatic OHOS
update channel is not configured.

## Verification limits

On 2026-10-24, Ability PR #109 at `b22ac744017d86cb6e7acddfc0f230126c536bff`
was built into the ARM64 Release library and ArkTS host, then installed on the
DevEco IDE MateBook Pro API 26 emulator. Its committed input, prompt-alpha,
prompt 0ms exit transition, and window-destruction fixes were used directly.
Regression covered selection, replacement, Backspace, in-app clipboard, Chinese
IME commit/deletion, shortcut chords, native menus, One Light/One Dark/Gruvbox
Light confirmations, immediate shortcut typeahead after prompt dismissal,
independent settings/workspace windows, focus return, normal close without
`app_will_quit` timeout, and unsaved-buffer restoration. The native save picker
and cancellation were also exercised.

Host regressions and SDK compilation do not establish complete native parity.
Creation-time input routing, task-center close/cancel visibility, independent
main-window non-activating launch and hardware-enforced provider permissions
remain separate acceptance items. The SDK does not promise `focus=false` for an
independent UIAbility main window.

The existing terminal implementation and tool packaging are retained; this
integration does not add terminal fixes. Native validation used a new dedicated
DevEco IDE emulator; that instance was stopped and deleted after regression.
