import { useEffect, useMemo, useRef, useState } from "react";
import { useDialogFocus } from "./useDialogFocus";
import {
  AlertTriangle,
  CheckCircle2,
  CircleAlert,
  Pencil,
  Search,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  UserPlus,
  X,
} from "lucide-react";

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
import { AdminFeedback } from "./AdminFeedback";
import { EmptyState, formatDateTime, paginate, TablePagination } from "./adminTableParts";
import "./account-workspace.css";

export type AdminTab = "users" | "spaces" | "feedback";

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
    if (!toast) return;
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
    const ranked = users.slice().sort((left, right) => sortTimestamp(right.created_at) - sortTimestamp(left.created_at));
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
    const ranked = spaces.slice().sort((left, right) => sortTimestamp(right.updated_at) - sortTimestamp(left.updated_at));
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
    if (userPage !== normalizedUserPage) setUserPage(normalizedUserPage);
  }, [normalizedUserPage, userPage]);

  useEffect(() => {
    if (spacePage !== normalizedSpacePage) setSpacePage(normalizedSpacePage);
  }, [normalizedSpacePage, spacePage]);

  async function handleSubmitUser(payload: AdminCreateUserPayload | AdminUpdateUserPayload, userId?: string) {
    if (saving) return;
    if (userId === currentUser.id) {
      payload = { ...payload, role: currentUser.role, is_active: currentUser.is_active };
    }
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
      setToast({ kind: "error", message: "保存用户失败，请检查错误提示。" });
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
    if (saving) return;
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
      setToast({ kind: "error", message: "保存空间失败，请检查错误提示。" });
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
    if (!confirmState || saving) return;
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
    <div className="account-admin-shell">
      {activeTab === "users" && (
        <section className="account-admin-surface" aria-labelledby="admin-users-title">
          <header className="account-admin-heading">
            <div>
              <h1 id="admin-users-title">用户管理</h1>
              <p>账户与访问权限</p>
            </div>
            <button type="button" className="account-button account-button-primary" onClick={() => setUserDrawer({ mode: "create", record: null })}>
              <UserPlus aria-hidden="true" />
              新增用户
            </button>
          </header>

          <div className="account-admin-toolbar">
            <label className="account-search-field">
              <Search aria-hidden="true" />
              <span className="account-sr-only">搜索用户</span>
              <input
                type="search"
                value={userSearch}
                onChange={(event) => setUserSearch(event.target.value)}
                placeholder="搜索用户名"
              />
            </label>
            <select className="account-select" value={userRoleFilter} onChange={(event) => setUserRoleFilter(event.target.value as "all" | "admin" | "user")} aria-label="筛选角色">
              <option value="all">全部角色</option>
              <option value="admin">管理员</option>
              <option value="user">普通用户</option>
            </select>
            <span className="account-admin-count">{filteredUsers.length} 位用户</span>
          </div>

          {usersError ? <div className="account-alert account-alert-error" role="alert">{usersError}</div> : null}
          <div className="account-admin-table-wrap" role="region" aria-label="用户列表" tabIndex={0}>
            <table className="account-admin-table account-user-table">
              <thead>
                <tr>
                  <th>用户名</th>
                  <th>角色</th>
                  <th>状态</th>
                  <th>创建时间</th>
                  <th><span className="account-sr-only">操作</span></th>
                </tr>
              </thead>
              <tbody>
                {loadingUsers ? (
                  <tr><td colSpan={5} className="account-admin-empty">正在读取用户列表...</td></tr>
                ) : filteredUsers.length === 0 ? (
                  <tr><td colSpan={5} className="account-admin-empty"><EmptyState title="当前没有匹配的用户记录" description="调整搜索条件，或创建一个新的普通用户账号。" /></td></tr>
                ) : (
                  pagedUsers.map((item) => (
                    <tr key={item.id}>
                      <td data-label="用户名">
                        <div className="account-admin-user-cell">
                          <span className="account-admin-avatar" aria-hidden="true">{item.username.slice(0, 1).toUpperCase()}</span>
                          <span className="account-admin-user-copy">
                            <strong title={item.username}>{item.username}</strong>
                            {item.display_name ? <small title={item.display_name}>{item.display_name}</small> : null}
                          </span>
                          {item.username === currentUser.username ? <span className="account-admin-self-badge">本人</span> : null}
                        </div>
                      </td>
                      <td data-label="角色"><span className={`account-admin-role account-admin-role-${item.role}`}>{item.role === "admin" ? "管理员" : "普通用户"}</span></td>
                      <td data-label="状态"><span className={`account-admin-status ${item.is_active ? "is-active" : "is-inactive"}`}><CheckCircle2 aria-hidden="true" />{item.is_active ? "正常" : "已停用"}</span></td>
                      <td data-label="创建时间">{formatDateTime(item.created_at)}</td>
                      <td data-label="操作" className="account-admin-actions-cell">
                        <div className="account-admin-actions">
                          <button type="button" className="account-icon-button" onClick={() => setUserDrawer({ mode: "edit", record: item })} title={`编辑 ${item.username}`} aria-label={`编辑 ${item.username}`}>
                            <Pencil aria-hidden="true" />
                          </button>
                          <button
                            type="button"
                            className="account-icon-button account-icon-button-danger"
                            disabled={saving || item.username === currentUser.username}
                            title={item.username === currentUser.username ? "不能删除当前登录账户" : `删除 ${item.username}`}
                            aria-label={item.username === currentUser.username ? "不能删除当前登录账户" : `删除 ${item.username}`}
                            onClick={() => setConfirmState({
                              type: "user",
                              title: "确认删除用户",
                              description: `用户「${item.username}」删除后不可恢复，且其个人模型配置、所属空间与关联会话产物会一并删除。`,
                              actionLabel: "删除用户",
                              target: item,
                            })}
                          >
                            <Trash2 aria-hidden="true" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          <TablePagination total={filteredUsers.length} page={normalizedUserPage} pageCount={userPageCount} pageSize={userPageSize} onPageChange={setUserPage} onPageSizeChange={setUserPageSize} />
        </section>
      )}
      {activeTab === "spaces" && (
        <section className="account-admin-surface" aria-labelledby="admin-spaces-title">
          <header className="account-admin-heading">
            <div>
              <h1 id="admin-spaces-title">资料空间</h1>
              <p>仅管理脱敏元数据，不开放原始内容</p>
            </div>
            <button
              type="button"
              className="account-button account-button-danger-outline"
              onClick={() => setConfirmState({
                type: "cleanup_orphans",
                title: "确认清理无主空间",
                description: "这会删除所有已经失去归属用户的空间，以及它们关联的会话文件与报告产物。",
                actionLabel: "清理无主空间",
              })}
              disabled={saving}
            >
              <ShieldAlert aria-hidden="true" />
              清理无主空间
            </button>
          </header>

          <div className="account-admin-toolbar">
            <label className="account-search-field">
              <Search aria-hidden="true" />
              <span className="account-sr-only">搜索空间</span>
              <input type="search" value={spaceSearch} onChange={(event) => setSpaceSearch(event.target.value)} placeholder="搜索空间或归属用户" />
            </label>
            <select className="account-select" value={spaceSourceFilter} onChange={(event) => setSpaceSourceFilter(event.target.value as "all" | "image" | "video" | "mixed")} aria-label="筛选来源">
              <option value="all">全部来源</option>
              <option value="image">图片</option>
              <option value="video">视频</option>
              <option value="mixed">混合</option>
            </select>
            <span className="account-admin-toolbar-note"><ShieldCheck aria-hidden="true" />已脱敏</span>
          </div>

          {spacesError ? <div className="account-alert account-alert-error" role="alert">{spacesError}</div> : null}
          <div className="account-admin-table-wrap" role="region" aria-label="空间列表" tabIndex={0}>
            <table className="account-admin-table account-space-table">
              <thead>
                <tr>
                  <th>归属用户</th>
                  <th>脱敏空间标识</th>
                  <th>来源</th>
                  <th>消息数</th>
                  <th>更新时间</th>
                  <th><span className="account-sr-only">操作</span></th>
                </tr>
              </thead>
              <tbody>
                {loadingSpaces ? (
                  <tr><td colSpan={6} className="account-admin-empty">正在读取空间列表...</td></tr>
                ) : filteredSpaces.length === 0 ? (
                  <tr><td colSpan={6} className="account-admin-empty"><EmptyState title="当前没有匹配的空间记录" description="这里只展示脱敏后的空间元数据。" /></td></tr>
                ) : (
                  pagedSpaces.map((item) => (
                    <tr key={item.session_id}>
                      <td data-label="归属用户">{item.owner_username ? <span title={item.owner_username}>{item.owner_username}</span> : <span className="account-admin-orphan-badge">无主空间</span>}</td>
                      <td data-label="脱敏空间标识"><span className="account-admin-space-title" title={item.title}>{item.title}</span></td>
                      <td data-label="来源"><SourceBadge sourceType={item.source_type} /></td>
                      <td data-label="消息数">{item.message_count}</td>
                      <td data-label="更新时间">{formatDateTime(item.updated_at)}</td>
                      <td data-label="操作" className="account-admin-actions-cell">
                        <div className="account-admin-actions">
                          <button type="button" className="account-icon-button" onClick={() => setSpaceDrawer(item)} title={`编辑 ${item.title}`} aria-label={`编辑 ${item.title}`}><Pencil aria-hidden="true" /></button>
                          <button type="button" className="account-icon-button account-icon-button-danger" disabled={saving} onClick={() => setConfirmState({ type: "space", title: "确认删除空间", description: `空间「${item.title}」删除后，用户将无法再从工作台访问这条会话。`, actionLabel: "删除空间", target: item })} title={`删除 ${item.title}`} aria-label={`删除 ${item.title}`}><Trash2 aria-hidden="true" /></button>
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          <TablePagination total={filteredSpaces.length} page={normalizedSpacePage} pageCount={spacePageCount} pageSize={spacePageSize} onPageChange={setSpacePage} onPageSizeChange={setSpacePageSize} />
        </section>
      )}
      {activeTab === "feedback" && <AdminFeedback />}

      {userDrawer ? <UserDrawer drawer={userDrawer} currentUserId={currentUser.id} saving={saving} errorMessage={usersError} onClose={() => { if (!saving) setUserDrawer(null); }} onSubmit={handleSubmitUser} /> : null}
      {spaceDrawer ? <SpaceDrawer space={spaceDrawer} users={users} saving={saving} errorMessage={spacesError} onClose={() => { if (!saving) setSpaceDrawer(null); }} onSubmit={handleSubmitSpace} /> : null}
      {confirmState ? <ConfirmDialog title={confirmState.title} description={confirmState.description} actionLabel={confirmState.actionLabel} saving={saving} onCancel={() => { if (!saving) setConfirmState(null); }} onConfirm={() => void handleConfirmAction()} /> : null}
      {toast ? <ToastBanner toast={toast} onClose={() => setToast(null)} /> : null}
    </div>
  );
}

function UserDrawer(props: {
  drawer: UserDrawerState;
  currentUserId: string;
  saving: boolean;
  errorMessage: string;
  onClose: () => void;
  onSubmit: (payload: AdminCreateUserPayload | AdminUpdateUserPayload, userId?: string) => Promise<void>;
}) {
  const { drawer, saving, errorMessage, onClose, onSubmit } = props;
  const dialogRef = useRef<HTMLElement>(null);
  useDialogFocus(dialogRef, Boolean(drawer));
  const [username, setUsername] = useState(drawer?.record?.username ?? "");
  const [displayName, setDisplayName] = useState(drawer?.record?.display_name ?? "");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"admin" | "user">(drawer?.record?.role ?? "user");
  const [isActive, setIsActive] = useState(drawer?.record?.is_active ?? true);
  const isEdit = drawer?.mode === "edit";
  const isSelf = drawer?.record?.id === props.currentUserId;

  useEffect(() => {
    setUsername(drawer?.record?.username ?? "");
    setDisplayName(drawer?.record?.display_name ?? "");
    setPassword("");
    setRole(drawer?.record?.role ?? "user");
    setIsActive(drawer?.record?.is_active ?? true);
  }, [drawer]);

  if (!drawer) return null;

  const submittedUserId = drawer.record?.id;
  const dirty = isEdit
    ? displayName !== (drawer.record?.display_name ?? "")
      || password !== ""
      || role !== (drawer.record?.role ?? "user")
      || isActive !== (drawer.record?.is_active ?? true)
    : username !== ""
      || displayName !== ""
      || password !== ""
      || role !== "user"
      || !isActive;

  function requestClose() {
    if (saving) return;
    if (dirty && !window.confirm("有未保存的用户信息，放弃这些修改？")) return;
    onClose();
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await onSubmit(
      isEdit
        ? { display_name: displayName.trim() || null, password: password.trim() || null, role, is_active: isActive }
        : { username: username.trim(), password: password.trim(), display_name: displayName.trim() || null, role, is_active: isActive },
      submittedUserId,
    );
  }

  return (
    <div className="account-drawer-overlay" onClick={requestClose}>
      <aside ref={dialogRef} className="account-drawer" onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-label={isEdit ? "编辑用户" : "新增用户"}>
        <header className="account-drawer-header">
          <div>
            <span className="account-drawer-kicker">{isEdit ? "编辑用户" : "新增用户"}</span>
            <h2>{isEdit ? username : "创建新用户"}</h2>
          </div>
          <button type="button" className="account-icon-button" disabled={saving} onClick={requestClose} aria-label="关闭用户编辑" title="关闭"><X aria-hidden="true" /></button>
        </header>
        <form className="account-drawer-form" onSubmit={(event) => void submit(event)}>
          <div className="account-drawer-body">
            {errorMessage ? <div className="account-alert account-alert-error" role="alert">{errorMessage}</div> : null}
            <label className="account-field">
              <span>用户名</span>
              <input className="account-input" type="text" value={username} maxLength={20} onChange={(event) => setUsername(event.target.value)} disabled={saving || isEdit} autoComplete="username" />
            </label>
            <label className="account-field">
              <span>显示名称</span>
              <input className="account-input" type="text" value={displayName} maxLength={48} onChange={(event) => setDisplayName(event.target.value)} disabled={saving} />
            </label>
            <label className="account-field">
              <span>{isEdit ? "重置密码" : "初始密码"}</span>
              <input className="account-input" type="password" value={password} maxLength={64} onChange={(event) => setPassword(event.target.value)} placeholder={isEdit ? "留空则不修改密码" : "至少 8 位"} disabled={saving} autoComplete={isEdit ? "new-password" : "new-password"} />
            </label>
            <label className="account-field">
              <span>角色</span>
              <select className="account-input" value={role} onChange={(event) => setRole(event.target.value as "admin" | "user")} disabled={saving || isSelf}>
                <option value="user">普通用户</option>
                <option value="admin">管理员</option>
              </select>
            </label>
            <label className="account-switch">
              <input type="checkbox" checked={isActive} onChange={(event) => setIsActive(event.target.checked)} disabled={saving || isSelf} />
              <span>允许登录</span>
            </label>
          </div>
          <footer className="account-drawer-footer">
            <span className="account-drawer-hint">{isEdit ? "留空密码则保留原密码。" : "新账号的权限由角色决定。"}</span>
            <div className="account-footer-actions">
              <button type="button" className="account-button account-button-secondary" onClick={requestClose} disabled={saving}>取消</button>
              <button type="submit" className="account-button account-button-primary" disabled={saving}>{saving ? "保存中..." : "保存"}</button>
            </div>
          </footer>
        </form>
      </aside>
    </div>
  );
}

function SpaceDrawer(props: {
  space: AdminSpaceRecord;
  users: AdminUserRecord[];
  saving: boolean;
  errorMessage: string;
  onClose: () => void;
  onSubmit: (payload: { ownerUserId: string; sortOrder: string }, sessionId: string) => Promise<void>;
}) {
  const { space, users, saving, errorMessage, onClose, onSubmit } = props;
  const dialogRef = useRef<HTMLElement>(null);
  useDialogFocus(dialogRef, true);
  const [ownerUserId, setOwnerUserId] = useState(space.owner_user_id ?? "");
  const [sortOrder, setSortOrder] = useState("");
  const ownerOptions = useMemo(() => {
    const activeUsers = users.filter((item) => item.is_active).sort((left, right) => left.username.localeCompare(right.username, "zh-CN"));
    if (!space.owner_user_id || activeUsers.some((item) => item.id === space.owner_user_id)) return activeUsers;
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

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await onSubmit({ ownerUserId, sortOrder }, space.session_id);
  }

  const dirty = ownerUserId !== (space.owner_user_id ?? "") || sortOrder.trim() !== "";

  function requestClose() {
    if (saving) return;
    if (dirty && !window.confirm("有未保存的空间调整，放弃这些修改？")) return;
    onClose();
  }

  return (
    <div className="account-drawer-overlay" onClick={requestClose}>
      <aside ref={dialogRef} className="account-drawer" onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-label="空间元数据编辑">
        <header className="account-drawer-header">
          <div>
            <span className="account-drawer-kicker">空间元数据编辑</span>
            <h2 title={space.title}>{space.title}</h2>
          </div>
          <button type="button" className="account-icon-button" disabled={saving} onClick={requestClose} aria-label="关闭空间编辑" title="关闭"><X aria-hidden="true" /></button>
        </header>
        <form className="account-drawer-form" onSubmit={(event) => void submit(event)}>
          <div className="account-drawer-body">
            {errorMessage ? <div className="account-alert account-alert-error" role="alert">{errorMessage}</div> : null}
            <p className="account-drawer-description">这里只允许调整归属与排序，不开放标题、正文和原始会话内容。</p>
            <label className="account-field">
              <span>归属用户</span>
              <select className="account-input" value={ownerUserId} onChange={(event) => setOwnerUserId(event.target.value)} disabled={saving}>
                <option value="">未指定</option>
                {ownerOptions.map((item) => <option key={item.id} value={item.id}>{item.username}{item.display_name ? ` / ${item.display_name}` : ""}</option>)}
              </select>
            </label>
            <label className="account-field">
              <span>排序号</span>
              <input className="account-input" type="number" value={sortOrder} onChange={(event) => setSortOrder(event.target.value)} placeholder="留空则不调整" disabled={saving} />
            </label>
            <div className="account-space-meta-grid">
              <div><span>空间标识</span><strong title={space.title}>{space.title}</strong></div>
              <div><span>来源类型</span><strong>{formatSourceType(space.source_type)}</strong></div>
              <div><span>消息数</span><strong>{space.message_count}</strong></div>
              <div><span>关联产物</span><strong>{space.linked_artifact_count}</strong></div>
              <div><span>更新时间</span><strong>{formatDateTime(space.updated_at)}</strong></div>
            </div>
          </div>
          <footer className="account-drawer-footer">
            <span className="account-drawer-hint">只有归属和排序会提交到管理 API。</span>
            <div className="account-footer-actions">
              <button type="button" className="account-button account-button-secondary" onClick={requestClose} disabled={saving}>取消</button>
              <button type="submit" className="account-button account-button-primary" disabled={saving}>{saving ? "保存中..." : "保存"}</button>
            </div>
          </footer>
        </form>
      </aside>
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
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef, true);
  return (
    <div className="account-modal-overlay" onClick={onCancel}>
      <div ref={dialogRef} className="account-modal" onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="account-confirm-title">
        <div className="account-modal-icon"><AlertTriangle aria-hidden="true" /></div>
        <div className="account-modal-copy">
          <strong id="account-confirm-title">{title}</strong>
          <p>{description}</p>
        </div>
        <div className="account-modal-actions">
          <button type="button" className="account-button account-button-secondary" onClick={onCancel} disabled={saving}>取消</button>
          <button type="button" className="account-button account-button-danger" onClick={onConfirm} disabled={saving}>{saving ? "处理中..." : actionLabel}</button>
        </div>
      </div>
    </div>
  );
}

function ToastBanner(props: { toast: NonNullable<ToastState>; onClose: () => void }) {
  const Icon = props.toast.kind === "success" ? CheckCircle2 : CircleAlert;
  return (
    <div className={`account-toast account-toast-${props.toast.kind}`} role="status" aria-live="polite">
      <Icon aria-hidden="true" />
      <span>{props.toast.message}</span>
      <button type="button" className="account-icon-button" onClick={props.onClose} aria-label="关闭提示" title="关闭"><X aria-hidden="true" /></button>
    </div>
  );
}

function sortTimestamp(value: string | number) {
  if (typeof value === "number") return value;
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return numeric;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function formatSourceType(sourceType?: string | null) {
  const normalized = (sourceType || "").trim().toLowerCase();
  if (!normalized) return "-";
  return SOURCE_LABELS[normalized] ?? normalized;
}

/** 来源徽标；没有来源时显示纯文本占位，不借用图片配色。 */
function SourceBadge(props: { sourceType?: string | null }) {
  const normalized = (props.sourceType || "").trim().toLowerCase();
  if (!normalized) return <>-</>;
  return <span className={`account-admin-source-badge ${resolveSourceBadgeClass(normalized)}`}>{formatSourceType(normalized)}</span>;
}

function resolveSourceBadgeClass(sourceType?: string | null) {
  const normalized = (sourceType || "").trim().toLowerCase();
  if (normalized === "video") return "account-source-video";
  if (normalized === "mixed") return "account-source-mixed";
  return "account-source-image";
}
