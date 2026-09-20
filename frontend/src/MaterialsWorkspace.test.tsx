// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MaterialsWorkspace } from "./MaterialsWorkspace";
import type { PendingUploadGroupState, PendingUploadItem } from "./uploadGroups";
import type { PublicAppConfig } from "./types";

const megabyte = 1024 * 1024;

const limits: PublicAppConfig["upload_limits"] = {
  max_total_bytes: 64 * megabyte,
  max_image_bytes: 2 * megabyte,
  max_video_bytes: 8 * megabyte,
  max_model_images: 48,
  max_images_per_group: 20,
  max_videos_per_group: 5,
  max_total_images: 20,
  max_total_videos: 5,
};

const groupDefinitions = [
  ["accident_overview", "事故参与方总体概况和损坏照片", 1],
  ["accident_videos", "视频", 2],
  ["injury_photos", "损伤信息照片", 3],
  ["vehicle_exterior", "车辆外部及外部损伤情况", 4],
  ["vehicle_interior", "车辆内部及内部损伤情况", 5],
  ["other_information", "其它信息", 6],
  ["scene", "现场", 7],
  ["privacy_photos", "隐私处理与截图", 8],
] as const;

function createItem(name: string, type: "image" | "video"): PendingUploadItem {
  const file = new File([`${name}-content`], name, {
    type: type === "image" ? "image/png" : "video/mp4",
  });
  return {
    id: `item-${name}`,
    file,
    mediaType: type,
    sizeBytes: file.size,
  };
}

function createGroups(itemsByGroup: Record<string, PendingUploadItem[]> = {}): PendingUploadGroupState[] {
  return groupDefinitions.map(([id, label, sequence]) => ({
    id,
    label,
    sequence,
    items: itemsByGroup[id] ?? [],
  }));
}

function renderWorkspace(
  groups: PendingUploadGroupState[],
  overrides: Partial<ComponentProps<typeof MaterialsWorkspace>> = {},
) {
  const props = {
    groups,
    limits,
    busy: false,
    onAdd: vi.fn(),
    onDropFiles: vi.fn(),
    onRemove: vi.fn(),
    onGenerate: vi.fn(),
    ...overrides,
  };
  return { ...render(<MaterialsWorkspace {...props} />), props };
}

function installObjectUrlMocks() {
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;
  const createObjectURL = vi.fn((file: File) => `blob:${file.name}`);
  const revokeObjectURL = vi.fn();

  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });

  return {
    createObjectURL,
    revokeObjectURL,
    restore() {
      if (originalCreateObjectURL) {
        Object.defineProperty(URL, "createObjectURL", { configurable: true, value: originalCreateObjectURL });
      } else {
        Reflect.deleteProperty(URL, "createObjectURL");
      }
      if (originalRevokeObjectURL) {
        Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: originalRevokeObjectURL });
      } else {
        Reflect.deleteProperty(URL, "revokeObjectURL");
      }
    },
  };
}

let restoreObjectUrls: (() => void) | null = null;

afterEach(() => {
  cleanup();
  restoreObjectUrls?.();
  restoreObjectUrls = null;
  vi.restoreAllMocks();
});

describe("MaterialsWorkspace", () => {
  it("显示全部加八类真实分组，并在全部模式明确默认添加归类", async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn();
    renderWorkspace(createGroups(), { onAdd });

    expect(screen.getByRole("button", { name: /^全部/ })).not.toBeNull();
    expect(screen.getByRole("button", { name: /^事故概况/ })).not.toBeNull();
    expect(screen.getByRole("button", { name: /^视频/ })).not.toBeNull();
    expect(screen.getByRole("button", { name: /^隐私处理/ })).not.toBeNull();
    expect(screen.getByText("添加归类：")).not.toBeNull();
    expect(screen.getAllByText("事故概况").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "添加资料到事故参与方总体概况和损坏照片" }));
    expect(onAdd).toHaveBeenCalledWith("accident_overview");

    await user.click(screen.getByRole("button", { name: /^车辆外部/ }));
    expect(screen.getAllByText("车辆外部").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "添加资料到车辆外部及外部损伤情况" }));
    expect(onAdd).toHaveBeenLastCalledWith("vehicle_exterior");
  });

  it("按当前分类把拖放文件交给父级，并使用真实限额显示", () => {
    const onDropFiles = vi.fn();
    const { container } = renderWorkspace(createGroups(), { onDropFiles });
    const droppedFile = new File(["drop"], "现场.png", { type: "image/png" });
    const surface = container.querySelector(".materials-surface");

    expect(surface).not.toBeNull();
    fireEvent.drop(surface as HTMLElement, {
      dataTransfer: { files: [droppedFile], types: ["Files"] },
    });

    expect(onDropFiles).toHaveBeenCalledWith("accident_overview", [droppedFile]);
    expect(screen.getByText("图片≤2.0 MB · 视频≤8.0 MB · 总量≤64 MB")).not.toBeNull();
  });

  it("生成缩略图，详情可预览视频，Escape关闭后恢复焦点", async () => {
    restoreObjectUrls = installObjectUrlMocks().restore;
    const user = userEvent.setup();
    const image = createItem("现场.png", "image");
    const video = createItem("行车记录.mp4", "video");
    renderWorkspace(
      createGroups({ accident_overview: [image], accident_videos: [video] }),
    );

    await waitFor(() => expect(document.querySelector('img[src="blob:现场.png"]')).not.toBeNull());
    const videoTrigger = screen.getByRole("button", { name: "打开资料详情：行车记录.mp4" });
    await user.click(videoTrigger);

    const dialog = screen.getByRole("dialog", { name: "行车记录.mp4" });
    expect((dialog.querySelector("video") as HTMLVideoElement).getAttribute("src")).toBe("blob:行车记录.mp4");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "关闭资料详情" }));

    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "行车记录.mp4" })).toBeNull();
    expect(document.activeElement).toBe(videoTrigger);
  });

  it("删除详情中的素材并在素材移除或卸载时释放ObjectURL", async () => {
    const objectUrls = installObjectUrlMocks();
    restoreObjectUrls = objectUrls.restore;
    const user = userEvent.setup();
    const item = createItem("待删.png", "image");
    const onRemove = vi.fn();
    const { rerender, unmount } = renderWorkspace(createGroups({ scene: [item] }), { onRemove });

    await waitFor(() => expect(objectUrls.createObjectURL).toHaveBeenCalledWith(item.file));
    const trigger = screen.getByRole("button", { name: "打开资料详情：待删.png" });
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "删除资料" }));
    expect(onRemove).toHaveBeenCalledWith("scene", item.id);

    rerender(<MaterialsWorkspace groups={createGroups()} limits={limits} busy={false} onAdd={vi.fn()} onDropFiles={vi.fn()} onRemove={onRemove} onGenerate={vi.fn()} />);
    await waitFor(() => expect(objectUrls.revokeObjectURL).toHaveBeenCalledWith("blob:待删.png"));

    const secondItem = createItem("卸载.png", "image");
    rerender(<MaterialsWorkspace groups={createGroups({ scene: [secondItem] })} limits={limits} busy={false} onAdd={vi.fn()} onDropFiles={vi.fn()} onRemove={onRemove} onGenerate={vi.fn()} />);
    await waitFor(() => expect(objectUrls.createObjectURL).toHaveBeenCalledWith(secondItem.file));
    unmount();
    expect(objectUrls.revokeObjectURL).toHaveBeenCalledWith("blob:卸载.png");
  });

  it("busy时阻断新增、删除和生成", async () => {
    const user = userEvent.setup();
    const item = createItem("忙碌.png", "image");
    const onAdd = vi.fn();
    const onRemove = vi.fn();
    const onGenerate = vi.fn();
    renderWorkspace(createGroups({ accident_overview: [item] }), {
      busy: true,
      onAdd,
      onRemove,
      onGenerate,
    });

    expect((screen.getByRole("button", { name: "添加资料" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "正在生成" }) as HTMLButtonElement).disabled).toBe(true);
    await user.click(screen.getByRole("button", { name: "打开资料详情：忙碌.png" }));
    expect((screen.getByRole("button", { name: "删除资料" }) as HTMLButtonElement).disabled).toBe(true);
    expect(onAdd).not.toHaveBeenCalled();
    expect(onRemove).not.toHaveBeenCalled();
    expect(onGenerate).not.toHaveBeenCalled();
  });
});
