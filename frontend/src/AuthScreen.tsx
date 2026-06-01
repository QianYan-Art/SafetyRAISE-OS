import { useMemo, useState } from "react";

type ThemeMode = "light" | "dark";
type AuthMode = "login" | "register";

interface AuthScreenProps {
  themeMode: ThemeMode;
  loading: boolean;
  errorMessage: string;
  onToggleTheme: () => void;
  onLogin: (payload: { username: string; password: string }) => Promise<void>;
  onRegister: (payload: { username: string; password: string; displayName?: string }) => Promise<void>;
}

const PASSWORD_MIN_LENGTH = 8;
type FieldErrorState = Partial<Record<"username" | "password" | "confirmPassword", string>>;

function SunIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2.5" />
      <path d="M12 19.5V22" />
      <path d="M4.93 4.93l1.77 1.77" />
      <path d="M17.3 17.3l1.77 1.77" />
      <path d="M2 12h2.5" />
      <path d="M19.5 12H22" />
      <path d="M4.93 19.07l1.77-1.77" />
      <path d="M17.3 6.7l1.77-1.77" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" />
    </svg>
  );
}

function EyeIcon(props: { open: boolean }) {
  if (props.open) {
    return (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z" />
        <circle cx="12" cy="12" r="3" />
      </svg>
    );
  }
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 3l18 18" />
      <path d="M10.58 10.58A2 2 0 0 0 12 14a2 2 0 0 0 1.42-.58" />
      <path d="M9.88 5.09A9.77 9.77 0 0 1 12 5c6.4 0 10 7 10 7a18.34 18.34 0 0 1-4.22 5.12" />
      <path d="M6.61 6.61C4.62 8.02 3.33 10.14 2 12c0 0 3.6 7 10 7 1.73 0 3.26-.51 4.56-1.24" />
    </svg>
  );
}

function resolvePasswordStrength(password: string) {
  const normalized = password.trim();
  const score = [
    normalized.length >= PASSWORD_MIN_LENGTH,
    /[A-Za-z]/.test(normalized),
    /\d/.test(normalized),
  ].filter(Boolean).length;

  if (score <= 1) {
    return { label: "弱", level: "weak" as const, filled: 1 };
  }
  if (score === 2) {
    return { label: "中", level: "medium" as const, filled: 2 };
  }
  return { label: "强", level: "strong" as const, filled: 3 };
}

export function AuthScreen(props: AuthScreenProps) {
  const { themeMode, loading, errorMessage, onToggleTheme, onLogin, onRegister } = props;
  const [mode, setMode] = useState<AuthMode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(true);
  const [localError, setLocalError] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrorState>({});
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);

  const passwordStrength = useMemo(() => resolvePasswordStrength(password), [password]);
  const mergedError = localError || errorMessage;

  async function handleSubmit() {
    setLocalError("");
    setFieldErrors({});
    const normalizedUsername = username.trim();
    const normalizedPassword = password.trim();
    if (!normalizedUsername || !normalizedPassword) {
      setFieldErrors({
        username: !normalizedUsername ? "请输入用户名。" : undefined,
        password: !normalizedPassword ? "请输入密码。" : undefined,
      });
      setLocalError("用户名和密码不能为空。");
      return;
    }

    if (mode === "register") {
      if (normalizedUsername.length < 4 || normalizedUsername.length > 20) {
        setFieldErrors({ username: "用户名长度必须在 4 到 20 个字符之间。" });
        setLocalError("用户名长度必须在 4 到 20 个字符之间。");
        return;
      }
      if (normalizedPassword.length < PASSWORD_MIN_LENGTH || !/[A-Za-z]/.test(normalizedPassword) || !/\d/.test(normalizedPassword)) {
        setFieldErrors({ password: "密码至少 8 位，且需包含字母和数字。" });
        setLocalError("密码至少 8 位，且需包含字母和数字。");
        return;
      }
      if (normalizedPassword !== confirmPassword.trim()) {
        setFieldErrors({ confirmPassword: "两次输入的密码不一致。" });
        setLocalError("两次输入的密码不一致。");
        return;
      }
      await onRegister({
        username: normalizedUsername,
        password: normalizedPassword,
        displayName: displayName.trim() || undefined,
      });
      return;
    }

    if (!rememberMe) {
      // 先保留交互入口，当前仍走统一 token 存储。
    }
    await onLogin({
      username: normalizedUsername,
      password: normalizedPassword,
    });
  }

  const isDarkMode = themeMode === "dark";

  return (
    <div className={`auth-screen theme-${themeMode}`}>
      <section className="auth-hero-panel">
        <div className="auth-hero-backdrop" />
        <div className="auth-hero-content">
          <span className="auth-hero-kicker">SafetyRAISE</span>
          <h1>道路交通事故分析报告生成系统</h1>
          <p>智能接收事故图片、视频与草稿信息，结合知识检索与专家指导意见生成可导出的分析研判文书。</p>
          <div className="auth-hero-route" aria-hidden="true">
            <div className="auth-hero-route-line" />
            <div className="auth-hero-route-stops">
              <div>
                <strong>现场材料</strong>
                <span>图片、视频、草稿分组进入工作区</span>
              </div>
              <div>
                <strong>结构分析</strong>
                <span>视觉识别、知识检索与专家判断并行收束</span>
              </div>
              <div>
                <strong>文书输出</strong>
                <span>报告、Word、PDF 归档导出</span>
              </div>
            </div>
          </div>
          <ul className="auth-hero-points">
            <li>多模态事故证据分组上传</li>
            <li>检索增强责任分析与定责支撑</li>
            <li>报告、Word、PDF 一体化导出</li>
          </ul>
        </div>
        <div className="auth-hero-footer">
          <span>© 2026 SafetyRAISE</span>
          <button
            type="button"
            className="theme-toggle-btn auth-theme-toggle"
            onClick={onToggleTheme}
            aria-label={isDarkMode ? "切换为浅色模式" : "切换为深色模式"}
            title={isDarkMode ? "切换为浅色模式" : "切换为深色模式"}
          >
            {isDarkMode ? <MoonIcon /> : <SunIcon />}
          </button>
        </div>
      </section>

      <section className="auth-form-panel">
        <div className="auth-form-shell">
          <div className="seg-tabs auth-seg-tabs" role="tablist" aria-label="登录注册切换">
            <button
              type="button"
              className={`seg-tab-btn ${mode === "login" ? "is-active" : ""}`}
              onClick={() => {
                setMode("login");
                setLocalError("");
                setFieldErrors({});
              }}
            >
              登录
            </button>
            <button
              type="button"
              className={`seg-tab-btn ${mode === "register" ? "is-active" : ""}`}
              onClick={() => {
                setMode("register");
                setLocalError("");
                setFieldErrors({});
              }}
            >
              注册
            </button>
          </div>

          <div className="auth-card">
            <div className="auth-card-header">
              <span className="auth-card-kicker">{mode === "login" ? "管理员与普通用户入口" : "创建普通用户账号"}</span>
              <h2>{mode === "login" ? "登录工作台" : "注册新账号"}</h2>
              <p>{mode === "login" ? "管理员登录后可进入用户管理与空间管理；普通用户登录后先配置自己的模型接入点。" : "当前仅支持用户名 + 密码注册，邮箱与手机号入口后续再补。"}</p>
            </div>

            {mergedError ? <div className="auth-alert auth-alert-error">{mergedError}</div> : null}

            <div className="form-field">
              <label htmlFor="auth-username">用户名</label>
              <input
                id="auth-username"
                className={`form-input ${fieldErrors.username ? "is-error" : ""}`}
                type="text"
                value={username}
                maxLength={20}
                onChange={(event) => {
                  setUsername(event.target.value);
                  setFieldErrors((current) => ({ ...current, username: undefined }));
                }}
                placeholder="请输入用户名"
                disabled={loading}
              />
              {fieldErrors.username ? <span className="field-error-text">{fieldErrors.username}</span> : null}
            </div>

            {mode === "register" ? (
              <div className="form-field">
                <label htmlFor="auth-display-name">显示名称 / 单位</label>
                <input
                  id="auth-display-name"
                  className="form-input"
                  type="text"
                  value={displayName}
                  maxLength={48}
                  onChange={(event) => setDisplayName(event.target.value)}
                  placeholder="可选，最多 48 个字符"
                  disabled={loading}
                />
              </div>
            ) : null}

            <div className="form-field">
              <label htmlFor="auth-password">密码</label>
              <div className={`input-with-action ${fieldErrors.password ? "is-error" : ""}`}>
                <input
                  id="auth-password"
                  className={`form-input ${fieldErrors.password ? "is-error" : ""}`}
                  type={showPassword ? "text" : "password"}
                  value={password}
                  maxLength={64}
                  onChange={(event) => {
                    setPassword(event.target.value);
                    setFieldErrors((current) => ({ ...current, password: undefined }));
                  }}
                  placeholder={mode === "login" ? "请输入密码" : "至少 8 位，需含字母和数字"}
                  disabled={loading}
                />
                <button
                  type="button"
                  className="input-action-btn"
                  onClick={() => setShowPassword((current) => !current)}
                  aria-label={showPassword ? "隐藏密码" : "显示密码"}
                  title={showPassword ? "隐藏密码" : "显示密码"}
                  disabled={loading}
                >
                  <EyeIcon open={showPassword} />
                </button>
              </div>
              {fieldErrors.password ? <span className="field-error-text">{fieldErrors.password}</span> : null}
            </div>

            {mode === "register" ? (
              <>
                <div className="password-strength">
                  <div className="password-strength-bars">
                    {[0, 1, 2].map((index) => (
                      <span
                        key={index}
                        className={`password-strength-bar ${passwordStrength.level} ${index < passwordStrength.filled ? "is-filled" : ""}`}
                      />
                    ))}
                  </div>
                  <span>密码强度：{passwordStrength.label}</span>
                </div>
                <div className="form-field">
                  <label htmlFor="auth-confirm-password">确认密码</label>
                  <div className={`input-with-action ${fieldErrors.confirmPassword ? "is-error" : ""}`}>
                    <input
                      id="auth-confirm-password"
                      className={`form-input ${fieldErrors.confirmPassword ? "is-error" : ""}`}
                      type={showConfirmPassword ? "text" : "password"}
                      value={confirmPassword}
                      maxLength={64}
                      onChange={(event) => {
                        setConfirmPassword(event.target.value);
                        setFieldErrors((current) => ({ ...current, confirmPassword: undefined }));
                      }}
                      placeholder="请再次输入密码"
                      disabled={loading}
                    />
                    <button
                      type="button"
                      className="input-action-btn"
                      onClick={() => setShowConfirmPassword((current) => !current)}
                      aria-label={showConfirmPassword ? "隐藏确认密码" : "显示确认密码"}
                      title={showConfirmPassword ? "隐藏确认密码" : "显示确认密码"}
                      disabled={loading}
                    >
                      <EyeIcon open={showConfirmPassword} />
                    </button>
                  </div>
                  {fieldErrors.confirmPassword ? <span className="field-error-text">{fieldErrors.confirmPassword}</span> : null}
                </div>
              </>
            ) : (
              <div className="auth-inline-meta">
                <label className="auth-checkbox">
                  <input
                    type="checkbox"
                    checked={rememberMe}
                    onChange={(event) => setRememberMe(event.target.checked)}
                    disabled={loading}
                  />
                  <span>记住我</span>
                </label>
                <button type="button" className="auth-link-btn" onClick={() => setLocalError("当前版本暂不支持自助找回密码，请联系管理员重置。")} disabled={loading}>
                  忘记密码？
                </button>
              </div>
            )}

            <button type="button" className="btn-primary auth-submit-btn" onClick={() => void handleSubmit()} disabled={loading}>
              {loading ? (
                <span className="auth-submit-content">
                  <span className="spinner" />
                  <span>{mode === "login" ? "登录中..." : "注册中..."}</span>
                </span>
              ) : (
                mode === "login" ? "登录" : "注册"
              )}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
