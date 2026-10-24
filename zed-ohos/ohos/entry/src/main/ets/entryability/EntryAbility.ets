import { LazyPlugin, NativeAbility } from "@ohos-rs/ability";
import { AppControlPlugin } from "@ohos-rs/ability-plugin-app-control";
import { ClipboardPlugin } from "@ohos-rs/ability-plugin-clipboard";
import { FilesPlugin } from "@ohos-rs/ability-plugin-files";
import { MenuPlugin } from "@ohos-rs/ability-plugin-menu";
import { NotificationPlugin } from "@ohos-rs/ability-plugin-notification";
import { PermissionPlugin } from "@ohos-rs/ability-plugin-permission";
import { SystemStatePlugin } from "@ohos-rs/ability-plugin-system-state";
import { UrlPlugin } from "@ohos-rs/ability-plugin-url";
import { WindowPlugin } from "@ohos-rs/ability-plugin-window";

export default class EntryAbility extends NativeAbility {
  public moduleName: string = "zed";
  public defaultPage: boolean = true;
  public loadMode: "sync" | "async" = "async";
  public independentWindowAbility: string = "WorkspaceAbility";
  public bridgePlugins = [
    new LazyPlugin(() => new AppControlPlugin()),
    new LazyPlugin(() => new ClipboardPlugin()),
    new LazyPlugin(() => new FilesPlugin()),
    // Zed resolves shortcuts against the focused editor, terminal and custom keymap.
    new LazyPlugin(() => new MenuPlugin({ handleKeyboardShortcuts: false, showMenuBar: false })),
    new LazyPlugin(() => new NotificationPlugin({ scheme: "gpui-notification" })),
    new LazyPlugin(() => new PermissionPlugin()),
    new LazyPlugin(() => new SystemStatePlugin()),
    new LazyPlugin(() => new UrlPlugin()),
    new LazyPlugin(() => new WindowPlugin()),
  ];
}
