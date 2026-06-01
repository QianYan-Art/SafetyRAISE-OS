import { useEffect, useMemo, useState } from "react";

import {
  cleanupAdminOrphanSpaces,
  createAdminUser,
  deleteAdminSpace,
  deleteAdminUser,
  formatApiErrorMessage,
  listAdminSpaces,
  listAdminUsers,
  updateAdminSpace,
  updateAdminUser,
} from "./api";
import type { AdminCreateUserPayload, AdminSpaceRecord, AdminUpdateUserPayload, AdminUserRecord, UserSummary } from "./types";

type AdminTab = "users" | "spaces";

interface AdminConsoleProps {
  currentUser: UserSummary;
  activeTab: AdminTab;
}

type UserDrawerState = {
  mode: "create" | "edit";
  record: AdminUserRecord | null;
} | null;

type SpaceDrawerState = AdminSpaceRecord | null;

type ConfirmState =
  | {
      type: "user";
      title: string;
      description: string;
      actionLabel: string;
      target: AdminUserRecord;
    }
  | {
      type: "space";
      title: string;
      description: string;
      actionLabel: string;
      target: AdminSpaceRecord;
    }
  | {
      type: "cleanup_orphans";
      title: string;
      description: string;
      actionLabel: string;
    }
  | null;

type ToastState =
  | {
      kind: "success" | "error";
      message: string;
    }
  | null;

const SOURCE_LABELS: Record<string, string> = {
  image: "图片",
  video: "视频",
  mixed: "混合",
};

const PAGE_SIZE_OPTIONS = [10, 20, 50] as const;

export function AdminConsole(props: AdminConsoleProps) {
  const { currentUser, activeTab } = props;
  const [users, setUsers] = useState<AdminUserRecord[]>([]);
  const [spaces, setSpaces] = useState<AdminSpaceRecord[]>([]);
  const [loadingUsers, setLoadingUsers] = useState(false);
  const [loadingSpaces, setLoadingSpaces] = useState(false);
  const [usersError, setUsersError] = useState("");
  const [spacesError, setSpacesError] = useState("");
  const [userSearch, setUserSearch] = useState("");
  const [userRoleFilter, setUserRoleFilter] = useState<"all" | "admin" | "user">("all");
  const [spaceSearch, setSpaceSearch] = useState("");
  const [spaceSourceFilter, setSpaceSourceFilter] = useState<"all" | "image" | "video" | "mixed">("all");
  const [userPage, setUserPage] = useState(1);
  const [spacePage, setSpacePage] = useState(1);
  const [userPageSize, setUserPageSize] = useState<number>(10);
  const [spacePageSize, setSpacePageSize] = useState<number>(20);
  const [userDrawer, setUserDrawer] = useState<UserDrawerState>(null);
  const [spaceDrawer, setSpaceDrawer] = useState<SpaceDrawerState>(null);
  const [confirmState, setConfirmState] = useState<ConfirmState>(null);
  const [toast, setToast] = useState<ToastState>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void refreshUsers();
    void refreshSpaces();
  }, []);

  useEffect(() => {
    if (!toast) {
      return;
    }
    const timeoutId = window.setTimeout(() => setToast(null), 3200);
    return () => window.clearTimeout(timeoutId);
  }, [toast]);

  useEffect(() => {
    setUserPage(1);
  }, [userSearch, userRoleFilter, userPageSize]);

  useEffect(() => {
    setSpacePage(1);
  }, [spaceSearch, spaceSourceFilter, spacePageSize]);

  async function refreshUsers() {
    setLoadingUsers(true);
    setUsersError("");
    try {
      setUsers(await listAdminUsers());
    } catch (error) {
      setUsersError(formatApiErrorMessage(error, "读取用户列表失败。"));
    } finally {
      setLoadingUsers(false);
    }
  }

  async function refreshSpaces() {
    setLoadingSpaces(true);
    setSpacesError("");
    try {
      setSpaces(await listAdminSpaces());
    } catch (error) {
      setSpacesError(formatApiErrorMessage(error, "读取空间列表失败。"));
    } finally {
      setLoadingSpaces(false);
    }
  }

  const filteredUsers = useMemo(() => {
    const query = userSearch.trim().toLowerCase();
    const ranked = users.slice().sort((left, right) => Number(right.created_at) - Number(left.created_at));
    return ranked.filter((item) => {
      const matchesQuery = !query
        || item.username.toLowerCase().includes(query)
        || (item.display_name ?? "").toLowerCase().includes(query);
      const matchesRole = userRoleFilter === "all" || item.role === userRoleFilter;
      return matchesQuery && matchesRole;
    });
  }, [userRoleFilter, userSearch, users]);

  const filteredSpaces = useMemo(() => {
    const query = spaceSearch.trim().toLowerCase();
    const ranked = spaces.slice().sort((left, right) => Number(right.updated_at) - Number(left.updated_at));
    return ranked.filter((item) => {
      const matchesQuery = !query
        || item.title.toLowerCase().includes(query)
        || (item.owner_username ?? "").toLowerCase().includes(query);
      const normalizedSource = (item.source_type ?? "").trim().toLowerCase();
      const matchesSource = spaceSourceFilter === "all" || normalizedSource === spaceSourceFilter;
      return matchesQuery && matchesSource;
    });
  }, [spaceSearch, spaceSourceFilter, spaces]);

  const userPageCount = Math.max(1, Math.ceil(filteredUsers.length / userPageSize));
  const spacePageCount = Math.max(1, Math.ceil(filteredSpaces.length / spacePageSize));
  const normalizedUserPage = Math.min(userPage, userPageCount);
  const normalizedSpacePage = Math.min(spacePage, spacePageCount);
  const pagedUsers = useMemo(
    () => paginate(filteredUsers, normalizedUserPage, userPageSize),
    [filteredUsers, normalizedUserPage, userPageSize],
  );
  const pagedSpaces = useMemo(
    () => paginate(filteredSpaces, normalizedSpacePage, spacePageSize),
    [filteredSpaces, normalizedSpacePage, spacePageSize],
  );

  useEffect(() => {
    if (userPage !== normalizedUserPage) {
      setUserPage(normalizedUserPage);
    }
  }, [normalizedUserPage, userPage]);

  useEffect(() => {
    if (spacePage !== normalizedSpacePage) {
      setSpacePage(normalizedSpacePage);
    }
  }, [normalizedSpacePage, spacePage]);

  async function handleSubmitUser(payload: AdminCreateUserPayload | AdminUpdateUserPayload, userId?: string) {
    setSaving(true);
    setUsersError("");
    try {
      if (userId) {
        await updateAdminUser(userId, payload);
        setToast({ kind: "success", message: "用户信息已更新。" });
      } else {
        await createAdminUser(payload as AdminCreateUserPayload);
        setToast({ kind: "success", message: "用户已创建。" });
      }
      setUserDrawer(null);
      await refreshUsers();
    } catch (error) {
      setUsersError(formatApiErrorMessage(error, "保存用户失败。"));
      setToast({ kind: "error", message: "保存用户失败，请重试。" });
    } finally {
      setSaving(false);
    }
  }

  async function handleDeleteUser(user: AdminUserRecord) {
    setSaving(true);
    setUsersError("");
    try {
      await deleteAdminUser(user.id);
      setToast({ kind: "success", message: `用户「${user.username}」已删除。` });
      await refreshUsers();
      await refreshSpaces();
    } catch (error) {
      setUsersError(formatApiErrorMessage(error, "删除用户失败。"));
      setToast({ kind: "error", message: "删除用户失败，请稍后重试。" });
    } finally {
      setSaving(false);
      setConfirmState(null);
    }
  }

  async function handleSubmitSpace(payload: { ownerUserId: string; sortOrder: string }, sessionId: string) {
    setSaving(true);
    setSpacesError("");
    try {
      await updateAdminSpace(sessionId, {
        owner_user_id: payload.ownerUserId.trim() || null,
        sort_order: payload.sortOrder.trim() ? Number(payload.sortOrder) : null,
      });
      setToast({ kind: "success", message: "空间元数据已更新。" });
      setSpaceDrawer(null);
      await refreshSpaces();
    } catch (error) {
      setSpacesError(formatApiErrorMessage(error, "保存空间失败。"));
      setToast({ kind: "error", message: "保存空间失败，请重试。" });
    } finally {
      setSaving(false);
    }
  }

  async function handleDeleteSpace(space: AdminSpaceRecord) {
    setSaving(true);
    setSpacesError("");
    try {
      await deleteAdminSpace(space.session_id);
      setToast({ kind: "success", message: `空间「${space.title}」已删除。` });
      await refreshSpaces();
    } catch (error) {
      setSpacesError(formatApiErrorMessage(error, "删除空间失败。"));
      setToast({ kind: "error", message: "删除空间失败，请稍后重试。" });
    } finally {
      setSaving(false);
      setConfirmState(null);
    }
  }

  async function handleCleanupOrphanSpaces() {
    setSaving(true);
    setSpacesError("");
    try {
      const response = await cleanupAdminOrphanSpaces();
      setToast({
        kind: "success",
        message: response.deleted_count > 0 ? `已清理 ${response.deleted_count} 个无主空间。` : "当前没有无主空间需要清理。",
      });
      await refreshSpaces();
    } catch (error) {
      setSpacesError(formatApiErrorMessage(error, "清理无主空间失败。"));
      setToast({ kind: "error", message: "清理无主空间失败，请稍后重试。" });
    } finally {
      setSaving(false);
      setConfirmState(null);
    }
  }

  async function handleConfirmAction() {
    if (!confirmState) {
      return;
    }
    if (confirmState.type === "user") {
      await handleDeleteUser(confirmState.target);
      return;
    }
    if (confirmState.type === "space") {
      await handleDeleteSpace(confirmState.target);
      return;
    }
    await handleCleanupOrphanSpaces();
  }

  return (
    <div className="admin-console-shell">
      <div className="admin-console-body">
        {activeTab === "users" ? (
          <section className="admin-surface">
            <div className="admin-toolbar">
              <div className="admin-toolbar-filters">
                <input
                  className="form-input admin-toolbar-search"
                  type="text"
                  placeholder="搜索用户名或显示名称"
                  value={userSearch}
                  onChange={(event) => setUserSearch(event.target.value)}
                />
                <select className="form-input admin-toolbar-select" value={userRoleFilter} onChange={(event) => setUserRoleFilter(event.target.value as "all" | "admin" | "user")}>
                  <option value="all">全部角色</option>
                  <option value="admin">管理员</option>
                  <option value="user">普通用户</option>
                </select>
              </div>
              <div className="admin-toolbar-actions">
                <button
                  type="button"
                  className="admin-circle-action admin-circle-action-primary"
                  onClick={() => setUserDrawer({ mode: "create", record: null })}
                  title="新增用户"
                  aria-label="新增用户"
                >
                  <UserPlusIcon />
                </button>
                <button
                  type="button"
                  className="admin-circle-action admin-circle-action-secondary"
                  onClick={() => {
                    setConfirmState({
                      type: "cleanup_orphans",
                      title: "确认清理无主空间",
                      description: "这会删除所有已经失去归属用户的空间，以及它们关联的会话文件与报告产物。",
                      actionLabel: "清理无主空间",
                    });
                  }}
                  disabled={saving}
                  title="清理无主空间"
                  aria-label="清理无主空间"
                >
                  <SweepIcon />
                </button>
              </div>
            </div>
            {usersError ? <div className="auth-alert auth-alert-error">{usersError}</div> : null}
            <div className="admin-table-shell">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>用户名</th>
                    <th>显示名称</th>
                    <th>角色</th>
                    <th>状态</th>
                    <th>创建时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {loadingUsers ? (
                    <tr>
                      <td colSpan={6} className="admin-table-empty">正在读取用户列表...</td>
                    </tr>
                  ) : filteredUsers.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="admin-table-empty">
                        <EmptyState
                          title="当前没有匹配的用户记录"
                          description="可以先调整搜索条件，或者直接创建一个新的普通用户账号。"
                        />
                      </td>
                    </tr>
                  ) : (
                    pagedUsers.map((item) => (
                      <tr key={item.id}>
                        <td>{item.username}</td>
                        <td>{item.display_name || "-"}</td>
                        <td><span className={`badge ${item.role === "admin" ? "badge-admin" : "badge-user"}`}>{item.role === "admin" ? "管理员" : "普通用户"}</span></td>
                        <td><span className={`badge ${item.is_active ? "badge-status-active" : "badge-status-inactive"}`}>{item.is_active ? "启用" : "停用"}</span></td>
                        <td>{formatDateTime(item.created_at)}</td>
                        <td>
                          <div className="admin-table-actions">
                            <button type="button" className="btn-secondary admin-mini-btn" onClick={() => setUserDrawer({ mode: "edit", record: item })}>编辑</button>
                            <button
                              type="button"
                              className="btn-danger admin-mini-btn admin-mini-btn-danger"
                              disabled={saving || item.username === currentUser.username}
                              onClick={() => {
                                setConfirmState({
                                  type: "user",
                                  title: "确认删除用户",
                                  description: `用户「${item.username}」删除后不可恢复，且其个人模型配置、所属空间与关联会话产物会一并删除。`,
                                  actionLabel: "删除用户",
                                  target: item,
                                });
                              }}
                            >
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
            <TablePagination
              total={filteredUsers.length}
              page={normalizedUserPage}
              pageCount={userPageCount}
              pageSize={userPageSize}
              onPageChange={setUserPage}
              onPageSizeChange={setUserPageSize}
            />
          </section>
        ) : (
          <section className="admin-surface">
            <div className="admin-toolbar">
              <div className="admin-toolbar-filters">
                <input
                  className="form-input admin-toolbar-search"
                  type="text"
                  placeholder="搜索脱敏标题或归属用户"
                  value={spaceSearch}
                  onChange={(event) => setSpaceSearch(event.target.value)}
                />
                <select className="form-input admin-toolbar-select" value={spaceSourceFilter} onChange={(event) => setSpaceSourceFilter(event.target.value as "all" | "image" | "video" | "mixed")}>
                  <option value="all">全部来源</option>
                  <option value="image">图片</option>
                  <option value="video">视频</option>
                  <option value="mixed">混合</option>
                </select>
              </div>
              <div className="admin-toolbar-note">
                <span className="badge badge-redacted">已脱敏</span>
                <strong>只显示元数据，不开放原文查看</strong>
              </div>
            </div>
            {spacesError ? <div className="auth-alert auth-alert-error">{spacesError}</div> : null}
            <div className="admin-table-shell">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>归属用户</th>
                    <th>脱敏空间标识</th>
                    <th>来源</th>
                    <th>消息数</th>
                    <th>更新时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {loadingSpaces ? (
                    <tr>
                      <td colSpan={6} className="admin-table-empty">正在读取空间列表...</td>
                    </tr>
                  ) : filteredSpaces.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="admin-table-empty">
                        <EmptyState
                          title="当前没有匹配的空间记录"
                          description="这里仅展示脱敏后的空间元数据，后续空间创建后会自动出现。"
                        />
                      </td>
                    </tr>
                  ) : (
                    pagedSpaces.map((item) => (
                      <tr key={item.session_id}>
                        <td>
                          {item.owner_username ? (
                            item.owner_username
                          ) : (
                            <span className="badge badge-orphan-space">无主空间</span>
                          )}
                        </td>
                        <td>{item.title}</td>
                        <td><span className={`badge ${resolveSourceBadgeClass(item.source_type)}`}>{formatSourceType(item.source_type)}</span></td>
                        <td>{item.message_count}</td>
                        <td>{formatDateTime(item.updated_at)}</td>
                        <td>
                          <div className="admin-table-actions">
                            <button type="button" className="btn-secondary admin-mini-btn" onClick={() => setSpaceDrawer(item)}>编辑</button>
                            <button
                              type="button"
                              className="btn-danger admin-mini-btn admin-mini-btn-danger"
                              disabled={saving}
                              onClick={() => {
                                setConfirmState({
                                  type: "space",
                                  title: "确认删除空间",
                                  description: `空间「${item.title}」删除后，用户将无法再从工作台访问这条会话。`,
                                  actionLabel: "删除空间",
                                  target: item,
                                });
                              }}
                            >
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
            <TablePagination
              total={filteredSpaces.length}
              page={normalizedSpacePage}
              pageCount={spacePageCount}
              pageSize={spacePageSize}
              onPageChange={setSpacePage}
              onPageSizeChange={setSpacePageSize}
            />
          </section>
        )}
      </div>

      {userDrawer ? (
        <UserDrawer
          drawer={userDrawer}
          saving={saving}
          onClose={() => setUserDrawer(null)}
          onSubmit={handleSubmitUser}
        />
      ) : null}

      {spaceDrawer ? (
        <SpaceDrawer
          space={spaceDrawer}
          users={users}
          saving={saving}
          onClose={() => setSpaceDrawer(null)}
          onSubmit={handleSubmitSpace}
        />
      ) : null}

      {confirmState ? (
        <ConfirmDialog
          title={confirmState.title}
          description={confirmState.description}
          actionLabel={confirmState.actionLabel}
          saving={saving}
          onCancel={() => setConfirmState(null)}
          onConfirm={() => void handleConfirmAction()}
        />
      ) : null}

      {toast ? <ToastBanner toast={toast} onClose={() => setToast(null)} /> : null}
    </div>
  );
}

function UserPlusIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M10 11.25a3.75 3.75 0 1 0 0-7.5 3.75 3.75 0 0 0 0 7.5Z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M3.5 19.25a6.5 6.5 0 0 1 13 0" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M18 8.5v5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M15.5 11h5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function SweepIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 18.5h8.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path d="m12 18.5 6.5-9.75-4.75-3.25L7.25 15.25" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M14.25 6.5 16 4.75" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M6.5 21h10.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function UserDrawer(props: {
  drawer: UserDrawerState;
  saving: boolean;
  onClose: () => void;
  onSubmit: (payload: AdminCreateUserPayload | AdminUpdateUserPayload, userId?: string) => Promise<void>;
}) {
  const { drawer, saving, onClose, onSubmit } = props;
  const [username, setUsername] = useState(drawer?.record?.username ?? "");
  const [displayName, setDisplayName] = useState(drawer?.record?.display_name ?? "");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"admin" | "user">(drawer?.record?.role ?? "user");
  const [isActive, setIsActive] = useState(drawer?.record?.is_active ?? true);
  const isEdit = drawer?.mode === "edit";

  useEffect(() => {
    setUsername(drawer?.record?.username ?? "");
    setDisplayName(drawer?.record?.display_name ?? "");
    setPassword("");
    setRole(drawer?.record?.role ?? "user");
    setIsActive(drawer?.record?.is_active ?? true);
  }, [drawer]);

  if (!drawer) {
    return null;
  }

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <aside className="drawer-shell" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div>
            <span className="drawer-kicker">{isEdit ? "编辑用户" : "新增用户"}</span>
            <h3>{isEdit ? username : "创建新用户"}</h3>
          </div>
          <button type="button" className="btn-icon drawer-close-btn" onClick={onClose} aria-label="关闭抽屉">×</button>
        </div>
        <div className="drawer-body">
          <div className="form-field">
            <label htmlFor="admin-user-username">用户名</label>
            <input
              id="admin-user-username"
              className="form-input"
              type="text"
              value={username}
              maxLength={20}
              onChange={(event) => setUsername(event.target.value)}
              disabled={saving || isEdit}
            />
          </div>
          <div className="form-field">
            <label htmlFor="admin-user-display-name">显示名称</label>
            <input
              id="admin-user-display-name"
              className="form-input"
              type="text"
              value={displayName}
              maxLength={48}
              onChange={(event) => setDisplayName(event.target.value)}
              disabled={saving}
            />
          </div>
          <div className="form-field">
            <label htmlFor="admin-user-password">{isEdit ? "重置密码" : "初始密码"}</label>
            <input
              id="admin-user-password"
              className="form-input"
              type="password"
              value={password}
              maxLength={64}
              onChange={(event) => setPassword(event.target.value)}
              placeholder={isEdit ? "留空则不修改密码" : "至少 8 位"}
              disabled={saving}
            />
          </div>
          <div className="form-field">
            <label htmlFor="admin-user-role">角色</label>
            <select id="admin-user-role" className="form-input" value={role} onChange={(event) => setRole(event.target.value as "admin" | "user")} disabled={saving}>
              <option value="user">普通用户</option>
              <option value="admin">管理员</option>
            </select>
          </div>
          <label className="auth-checkbox admin-switch-line">
            <input type="checkbox" checked={isActive} onChange={(event) => setIsActive(event.target.checked)} disabled={saving} />
            <span>账号启用</span>
          </label>
        </div>
        <div className="drawer-footer">
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>取消</button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => void onSubmit(
              isEdit
                ? {
                    display_name: displayName.trim() || null,
                    password: password.trim() || null,
                    role,
                    is_active: isActive,
                  }
                : {
                    username: username.trim(),
                    password: password.trim(),
                    display_name: displayName.trim() || null,
                    role,
                    is_active: isActive,
                  },
              drawer.record?.id,
            )}
            disabled={saving}
          >
            {saving ? "保存中..." : "保存"}
          </button>
        </div>
      </aside>
    </div>
  );
}

function SpaceDrawer(props: {
  space: AdminSpaceRecord;
  users: AdminUserRecord[];
  saving: boolean;
  onClose: () => void;
  onSubmit: (payload: { ownerUserId: string; sortOrder: string }, sessionId: string) => Promise<void>;
}) {
  const { space, users, saving, onClose, onSubmit } = props;
  const [ownerUserId, setOwnerUserId] = useState(space.owner_user_id ?? "");
  const [sortOrder, setSortOrder] = useState("");
  const ownerOptions = useMemo(() => {
    const activeUsers = users
      .filter((item) => item.is_active)
      .sort((left, right) => left.username.localeCompare(right.username, "zh-CN"));
    if (!space.owner_user_id || activeUsers.some((item) => item.id === space.owner_user_id)) {
      return activeUsers;
    }
    return [
      ...activeUsers,
      {
        id: space.owner_user_id,
        username: space.owner_username || "未知用户",
        display_name: null,
        role: "user" as const,
        is_active: false,
        created_at: "",
        updated_at: "",
      },
    ];
  }, [space.owner_user_id, space.owner_username, users]);

  useEffect(() => {
    setOwnerUserId(space.owner_user_id ?? "");
    setSortOrder("");
  }, [space]);

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <aside className="drawer-shell" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div>
            <span className="drawer-kicker">空间元数据编辑</span>
            <h3>{space.title}</h3>
            <p>这里只允许调整归属与排序，不开放标题、正文和原始会话内容。</p>
          </div>
          <button type="button" className="btn-icon drawer-close-btn" onClick={onClose} aria-label="关闭抽屉">×</button>
        </div>
        <div className="drawer-body">
          <div className="form-field">
            <label htmlFor="admin-space-owner">归属用户</label>
            <select
              id="admin-space-owner"
              className="form-input"
              value={ownerUserId}
              onChange={(event) => setOwnerUserId(event.target.value)}
              disabled={saving}
            >
              <option value="">未指定</option>
              {ownerOptions.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.username}{item.display_name ? ` / ${item.display_name}` : ""}
                </option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="admin-space-sort-order">排序号</label>
            <input
              id="admin-space-sort-order"
              className="form-input"
              type="number"
              value={sortOrder}
              onChange={(event) => setSortOrder(event.target.value)}
              placeholder="留空则不调整"
              disabled={saving}
            />
          </div>
          <div className="admin-space-meta-grid">
            <div>
              <span>空间标识</span>
              <strong>{space.title}</strong>
            </div>
            <div>
              <span>来源类型</span>
              <strong>{formatSourceType(space.source_type)}</strong>
            </div>
            <div>
              <span>消息数</span>
              <strong>{space.message_count}</strong>
            </div>
            <div>
              <span>关联产物</span>
              <strong>{space.linked_artifact_count}</strong>
            </div>
            <div>
              <span>更新时间</span>
              <strong>{formatDateTime(space.updated_at)}</strong>
            </div>
          </div>
        </div>
        <div className="drawer-footer">
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>取消</button>
          <button type="button" className="btn-primary" onClick={() => void onSubmit({ ownerUserId, sortOrder }, space.session_id)} disabled={saving}>
            {saving ? "保存中..." : "保存"}
          </button>
        </div>
      </aside>
    </div>
  );
}

function EmptyState(props: { title: string; description: string }) {
  const { title, description } = props;
  return (
    <div className="empty-state">
      <div className="empty-state-icon" aria-hidden="true">○</div>
      <strong>{title}</strong>
      <span>{description}</span>
    </div>
  );
}

function ConfirmDialog(props: {
  title: string;
  description: string;
  actionLabel: string;
  saving: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { title, description, actionLabel, saving, onCancel, onConfirm } = props;
  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-card" onClick={(event) => event.stopPropagation()}>
        <div className="modal-card-header">
          <strong>{title}</strong>
          <p>{description}</p>
        </div>
        <div className="modal-card-actions">
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={saving}>取消</button>
          <button type="button" className="btn-danger" onClick={onConfirm} disabled={saving}>{saving ? "处理中..." : actionLabel}</button>
        </div>
      </div>
    </div>
  );
}

function ToastBanner(props: { toast: NonNullable<ToastState>; onClose: () => void }) {
  const { toast, onClose } = props;
  return (
    <div className={`toast-banner is-${toast.kind}`} role="status" aria-live="polite">
      <span>{toast.message}</span>
      <button type="button" className="btn-icon toast-close-btn" onClick={onClose} aria-label="关闭提示">×</button>
    </div>
  );
}

function formatDateTime(value: string | number) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  const hours = `${date.getHours()}`.padStart(2, "0");
  const minutes = `${date.getMinutes()}`.padStart(2, "0");
  return `${year}-${month}-${day} ${hours}:${minutes}`;
}

function formatSourceType(sourceType?: string | null) {
  const normalized = (sourceType || "").trim().toLowerCase();
  if (!normalized) {
    return "-";
  }
  return SOURCE_LABELS[normalized] ?? normalized;
}

function resolveSourceBadgeClass(sourceType?: string | null) {
  const normalized = (sourceType || "").trim().toLowerCase();
  if (normalized === "video") {
    return "badge-source-video";
  }
  if (normalized === "mixed") {
    return "badge-source-mixed";
  }
  return "badge-source-image";
}

function paginate<T>(items: T[], page: number, pageSize: number): T[] {
  const safePage = Math.max(1, page);
  const start = (safePage - 1) * pageSize;
  return items.slice(start, start + pageSize);
}

function TablePagination(props: {
  total: number;
  page: number;
  pageCount: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
}) {
  const { total, page, pageCount, pageSize, onPageChange, onPageSizeChange } = props;
  return (
    <div className="admin-pagination">
      <span className="admin-pagination-total">共 {total} 条</span>
      <div className="admin-pagination-controls">
        <label className="admin-pagination-size">
          <span>每页</span>
          <select className="form-input admin-toolbar-select admin-page-size-select" value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))}>
            {PAGE_SIZE_OPTIONS.map((option) => (
              <option key={option} value={option}>{option}</option>
            ))}
          </select>
        </label>
        <button type="button" className="btn-secondary admin-page-btn" onClick={() => onPageChange(Math.max(1, page - 1))} disabled={page <= 1}>
          上一页
        </button>
        <span className="admin-pagination-current">{page} / {pageCount}</span>
        <button type="button" className="btn-secondary admin-page-btn" onClick={() => onPageChange(Math.min(pageCount, page + 1))} disabled={page >= pageCount}>
          下一页
        </button>
      </div>
    </div>
  );
}
