import type { PublicAppConfig } from "./types";

export interface UploadGroupDefinition {
  id: string;
  label: string;
  subtitle?: string;
  sequence: number;
}

export interface PendingUploadItem {
  id: string;
  file: File;
  mediaType: "image" | "video";
  sizeBytes: number;
}

export interface PendingUploadGroupState extends UploadGroupDefinition {
  items: PendingUploadItem[];
}

export interface UploadSelectionValidationResult {
  blockingMessage: string | null;
  noticeMessage: string | null;
}

export const UPLOAD_GROUP_DEFINITIONS: UploadGroupDefinition[] = [
  { id: "accident_overview", label: "事故参与方总体概况和损坏照片", sequence: 1 },
  { id: "accident_videos", label: "视频", subtitle: "Accident videos", sequence: 2 },
  { id: "injury_photos", label: "损伤信息照片", subtitle: "Injury information", sequence: 3 },
  { id: "vehicle_exterior", label: "车辆外部及外部损伤情况", sequence: 4 },
  { id: "vehicle_interior", label: "车辆内部及内部损伤情况", sequence: 5 },
  { id: "other_information", label: "其它信息", sequence: 6 },
  { id: "scene", label: "现场", sequence: 7 },
  { id: "privacy_photos", label: "隐私处理与截图", subtitle: "车牌、姓名、住址、电话、证件等", sequence: 8 },
];

function createPendingUploadItemId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `pending-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function createInitialPendingUploadGroups(): PendingUploadGroupState[] {
  return UPLOAD_GROUP_DEFINITIONS.map((group) => ({
    ...group,
    items: [],
  }));
}

export function detectClientMediaType(file: File): "image" | "video" | null {
  const normalizedType = file.type.toLowerCase();
  if (normalizedType.startsWith("image/")) {
    return "image";
  }
  if (normalizedType.startsWith("video/")) {
    return "video";
  }

  const lowerName = file.name.toLowerCase();
  if (/\.(png|jpe?g|webp|bmp|gif|heic|heif)$/i.test(lowerName)) {
    return "image";
  }
  if (/\.(mp4|mov|avi|mkv|webm|m4v)$/i.test(lowerName)) {
    return "video";
  }
  return null;
}

export function getPendingUploadStats(groups: PendingUploadGroupState[]) {
  let totalBytes = 0;
  let totalImages = 0;
  let totalVideos = 0;
  let activeGroupCount = 0;

  groups.forEach((group) => {
    if (group.items.length > 0) {
      activeGroupCount += 1;
    }
    group.items.forEach((item) => {
      totalBytes += item.sizeBytes;
      if (item.mediaType === "image") {
        totalImages += 1;
      } else {
        totalVideos += 1;
      }
    });
  });

  return {
    totalBytes,
    totalImages,
    totalVideos,
    activeGroupCount,
    totalFiles: totalImages + totalVideos,
  };
}

export function hasPendingUploads(groups: PendingUploadGroupState[]): boolean {
  return groups.some((group) => group.items.length > 0);
}

export function appendPendingFiles(
  groups: PendingUploadGroupState[],
  groupId: string,
  files: File[],
): PendingUploadGroupState[] {
  return groups.map((group) =>
    group.id === groupId
      ? {
          ...group,
          items: [
            ...group.items,
            ...files.map((file) => ({
              id: createPendingUploadItemId(),
              file,
              mediaType: detectClientMediaType(file) as "image" | "video",
              sizeBytes: file.size,
            })),
          ],
        }
      : group,
  );
}

export function removePendingUploadItem(
  groups: PendingUploadGroupState[],
  groupId: string,
  itemId: string,
): PendingUploadGroupState[] {
  return groups.map((group) =>
    group.id === groupId
      ? {
          ...group,
          items: group.items.filter((item) => item.id !== itemId),
        }
      : group,
  );
}

export function clearPendingUploadGroups(): PendingUploadGroupState[] {
  return createInitialPendingUploadGroups();
}

export function validatePendingUploadSelection(
  existingGroups: PendingUploadGroupState[],
  groupId: string,
  files: File[],
  uploadLimits: PublicAppConfig["upload_limits"],
): UploadSelectionValidationResult {
  if (files.length === 0) {
    return { blockingMessage: "请先选择图片或视频文件。", noticeMessage: null };
  }

  const targetGroup = existingGroups.find((group) => group.id === groupId);
  if (!targetGroup) {
    return { blockingMessage: "未找到目标上传分组。", noticeMessage: null };
  }

  const currentStats = getPendingUploadStats(existingGroups);
  const currentGroupImages = targetGroup.items.filter((item) => item.mediaType === "image").length;
  const currentGroupVideos = targetGroup.items.filter((item) => item.mediaType === "video").length;
  let incomingImages = 0;
  let incomingVideos = 0;
  let incomingBytes = 0;

  for (const file of files) {
    const mediaType = detectClientMediaType(file);
    if (!mediaType) {
      return {
        blockingMessage: `不支持的文件类型：${file.name}。`,
        noticeMessage: null,
      };
    }
    if (mediaType === "image") {
      incomingImages += 1;
      if (file.size > uploadLimits.max_image_bytes) {
        return {
          blockingMessage: `图片 ${file.name} 大小不能超过 ${Math.round(uploadLimits.max_image_bytes / 1024 / 1024)}MB。`,
          noticeMessage: null,
        };
      }
    } else {
      incomingVideos += 1;
      if (file.size > uploadLimits.max_video_bytes) {
        return {
          blockingMessage: `视频 ${file.name} 大小不能超过 ${Math.round(uploadLimits.max_video_bytes / 1024 / 1024)}MB。`,
          noticeMessage: null,
        };
      }
    }
    incomingBytes += file.size;
  }

  if (currentGroupImages + incomingImages > uploadLimits.max_images_per_group) {
    return {
      blockingMessage: `分组“${targetGroup.label}”最多只能放入 ${uploadLimits.max_images_per_group} 张图片。`,
      noticeMessage: null,
    };
  }
  if (currentGroupVideos + incomingVideos > uploadLimits.max_videos_per_group) {
    return {
      blockingMessage: `分组“${targetGroup.label}”最多只能放入 ${uploadLimits.max_videos_per_group} 个视频。`,
      noticeMessage: null,
    };
  }
  if (currentStats.totalImages + incomingImages > uploadLimits.max_total_images) {
    return {
      blockingMessage: `当前会话全部分组的图片总数不能超过 ${uploadLimits.max_total_images} 张。`,
      noticeMessage: null,
    };
  }
  if (currentStats.totalVideos + incomingVideos > uploadLimits.max_total_videos) {
    return {
      blockingMessage: `当前会话全部分组的视频总数不能超过 ${uploadLimits.max_total_videos} 个。`,
      noticeMessage: null,
    };
  }
  if (currentStats.totalBytes + incomingBytes > uploadLimits.max_total_bytes) {
    return {
      blockingMessage: `当前会话上传总大小不能超过 ${Math.round(uploadLimits.max_total_bytes / 1024 / 1024)}MB。`,
      noticeMessage: null,
    };
  }

  return { blockingMessage: null, noticeMessage: null };
}

export function buildGroupedUploadPayload(groups: PendingUploadGroupState[]) {
  const files: File[] = [];
  const items: Array<{
    category_id: string;
    original_name: string;
    media_type: "image" | "video";
    sequence: number;
    group_sequence: number;
  }> = [];
  let globalSequence = 1;

  groups
    .slice()
    .sort((left, right) => left.sequence - right.sequence)
    .forEach((group) => {
      group.items.forEach((item, index) => {
        files.push(item.file);
        items.push({
          category_id: group.id,
          original_name: item.file.name,
          media_type: item.mediaType,
          sequence: globalSequence,
          group_sequence: index + 1,
        });
        globalSequence += 1;
      });
    });

  return {
    files,
    uploadManifest: {
      groups: groups.map((group) => ({
        category_id: group.id,
        category_label: group.label,
        category_subtitle: group.subtitle,
        sequence: group.sequence,
      })),
      items,
    },
  };
}
