import { useId, useMemo, useState, type FormEvent } from "react";
import "./auth-screen.css";

type ThemeMode = "light" | "dark";
type AuthMode = "login" | "register";

interface AuthScreenProps {
  themeMode: ThemeMode;
  loading: boolean;
  errorMessage: string;
  /** 切换登录/注册时清掉上层保留的上一次请求错误。 */
  onClearError?: () => void;
  onToggleTheme: () => void;
  onLogin: (payload: { username: string; password: string }) => Promise<void>;
  onRegister: (payload: { username: string; password: string; displayName?: string }) => Promise<void>;
}

const PASSWORD_MIN_LENGTH = 8;
const WORKFLOW_STEPS = ["整理资料", "核对事实", "查看报告"] as const;
type FieldName = "username" | "password" | "confirmPassword";
type FieldErrorState = Partial<Record<FieldName, string>>;

const MODE_COPY: Record<AuthMode, { title: string; description: string; submit: string; pending: string }> = {
  login: {
    title: "登录工作台",
    description: "使用管理员分配或自行注册的账号登录。",
    submit: "登录",
    pending: "正在登录…",
  },
  register: {
    title: "注册新账号",
    description: "注册为普通用户。首次进入工作台时，需要先填写视觉与报告模型的接入配置。",
    submit: "注册",
    pending: "正在注册…",
  },
};

function SunIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2.5M12 19.5V22M4.93 4.93l1.77 1.77M17.3 17.3l1.77 1.77M2 12h2.5M19.5 12H22M4.93 19.07l1.77-1.77M17.3 6.7l1.77-1.77" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" />
    </svg>
  );
}

function EyeIcon(props: { open: boolean }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {props.open ? (
        <>
          <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z" />
          <circle cx="12" cy="12" r="3" />
        </>
      ) : (
        <path d="M3 3l18 18M10.58 10.58A2 2 0 0 0 12 14a2 2 0 0 0 1.42-.58M9.88 5.09A9.77 9.77 0 0 1 12 5c6.4 0 10 7 10 7a18.34 18.34 0 0 1-4.22 5.12M6.61 6.61C4.62 8.02 3.33 10.14 2 12c0 0 3.6 7 10 7 1.73 0 3.26-.51 4.56-1.24" />
      )}
    </svg>
  );
}

/** 品牌区的事故现场示意图：十字路口、车道与停止线、人行横道、两车轨迹与碰撞点，按交警现场图的画法附指北针与测距。纯装饰。 */
function SceneSketch() {
  return (
    <svg className="auth-sketch" viewBox="0 0 440 300" aria-hidden="true" focusable="false">
      <g className="auth-sketch-curb">
        <path d="M0 110H164Q180 110 180 94V0" />
        <path d="M260 0V94Q260 110 276 110H440" />
        <path d="M0 190H164Q180 190 180 206V300" />
        <path d="M260 300V206Q260 190 276 190H440" />
      </g>
      <g className="auth-sketch-lane">
        <path d="M0 150H124M290 150H440M220 0V96M220 222V300" />
      </g>
      <g className="auth-sketch-stop">
        <path d="M168 152V188M222 214H258" />
      </g>
      <g className="auth-sketch-zebra">
        {[116, 126, 136, 146, 156, 166, 176].map((y) => <rect key={y} x="132" y={y} width="22" height="5" rx="1" />)}
      </g>
      <g className="auth-sketch-measure">
        <path d="M168 140V152M231 140V160" className="auth-sketch-extension" />
        <path d="M168 136H231M168 131V141M231 131V141" />
        <text x="199.5" y="126" textAnchor="middle">12.6 m</text>
      </g>
      <g className="auth-sketch-north">
        <path d="M408 46V16M401 25L408 16L415 25" />
        <text x="408" y="62" textAnchor="middle">N</text>
      </g>
      <g className="auth-sketch-path">
        <path d="M24 170H186M242 290V206" />
      </g>
      <g className="auth-sketch-vehicle auth-sketch-vehicle-a">
        <rect x="192" y="161" width="38" height="18" rx="4" />
        <text x="211" y="198" textAnchor="middle">①</text>
      </g>
      <g className="auth-sketch-vehicle auth-sketch-vehicle-b">
        <rect x="233" y="164" width="18" height="36" rx="4" />
        <text x="270" y="190" textAnchor="middle">②</text>
      </g>
      <g className="auth-sketch-impact">
        <circle cx="232" cy="168" r="18" />
      </g>
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
  if (score <= 1) return { label: "弱", level: "weak" as const, filled: 1 };
  if (score === 2) return { label: "中", level: "medium" as const, filled: 2 };
  return { label: "强", level: "strong" as const, filled: 3 };
}

export function AuthScreen(props: AuthScreenProps) {
  const { themeMode, loading, errorMessage, onClearError, onToggleTheme, onLogin, onRegister } = props;
  const [mode, setMode] = useState<AuthMode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [localError, setLocalError] = useState("");
  const [notice, setNotice] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrorState>({});
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const idPrefix = useId();
  const headingId = `${idPrefix}-heading`;

  const passwordStrength = useMemo(() => resolvePasswordStrength(password), [password]);
  const mergedError = localError || errorMessage;
  const copy = MODE_COPY[mode];
  const isRegister = mode === "register";
  const isDarkMode = themeMode === "dark";
  const themeLabel = isDarkMode ? "切换为浅色模式" : "切换为深色模式";

  function switchMode(next: AuthMode) {
    setMode(next);
    setLocalError("");
    setNotice("");
    setFieldErrors({});
    onClearError?.();
  }

  function rejectField(field: FieldName, message: string) {
    setFieldErrors({ [field]: message });
    setLocalError(message);
  }

  function clearFieldError(field: FieldName) {
    setFieldErrors((current) => ({ ...current, [field]: undefined }));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (loading) return;
    setLocalError("");
    setNotice("");
    setFieldErrors({});
    const normalizedUsername = username.trim();
    const normalizedPassword = password.trim();
    if (!normalizedUsername || !normalizedPassword) {
      setFieldErrors({
        username: normalizedUsername ? undefined : "请输入用户名。",
        password: normalizedPassword ? undefined : "请输入密码。",
      });
      setLocalError("用户名和密码不能为空。");
      return;
    }

    if (!isRegister) {
      await onLogin({ username: normalizedUsername, password: normalizedPassword });
      return;
    }
    if (normalizedUsername.length < 4 || normalizedUsername.length > 20) {
      rejectField("username", "用户名长度必须在 4 到 20 个字符之间。");
      return;
    }
    if (normalizedPassword.length < PASSWORD_MIN_LENGTH || !/[A-Za-z]/.test(normalizedPassword) || !/\d/.test(normalizedPassword)) {
      rejectField("password", "密码至少 8 位，且需包含字母和数字。");
      return;
    }
    if (normalizedPassword !== confirmPassword.trim()) {
      rejectField("confirmPassword", "两次输入的密码不一致。");
      return;
    }
    await onRegister({
      username: normalizedUsername,
      password: normalizedPassword,
      displayName: displayName.trim() || undefined,
    });
  }

  const describedBy = (field: FieldName, hintId?: string) =>
    [fieldErrors[field] ? `${idPrefix}-${field}-error` : "", hintId ?? ""].filter(Boolean).join(" ") || undefined;
  const fieldError = (field: FieldName) =>
    fieldErrors[field] ? <span id={`${idPrefix}-${field}-error`} className="auth-field-error">{fieldErrors[field]}</span> : null;

  return (
    <div className={`auth-screen theme-${themeMode}`}>
      <section className="auth-brand" aria-label="系统介绍">
        <span className="auth-brand-name">SafetyRAISE</span>
        <figure className="auth-scene">
          <SceneSketch />
          <figcaption>现场示意 · 比例 1:200</figcaption>
        </figure>
        <div className="auth-brand-copy">
          <h1><span>道路交通事故</span><span>分析报告生成系统</span></h1>
          <p className="auth-brand-lead">从现场资料到分析报告，每一步都能在工作台里核对和修改。</p>
          <ol className="auth-steps" aria-label="处理流程">
            {WORKFLOW_STEPS.map((step, index) => (
              <li key={step}><span className="auth-step-index">{index + 1}</span>{step}</li>
            ))}
          </ol>
        </div>
        <span className="auth-brand-footer">© 2026 SafetyRAISE</span>
      </section>

      <section className="auth-panel">
        <div className="auth-panel-bar">
          <button type="button" className="auth-theme-toggle" onClick={onToggleTheme} aria-label={themeLabel} title={themeLabel}>
            {isDarkMode ? <MoonIcon /> : <SunIcon />}
          </button>
        </div>

        <form className="auth-form" aria-labelledby={headingId} noValidate onSubmit={(event) => void handleSubmit(event)}>
          <div className="auth-switch" role="group" aria-label="登录或注册">
            <button type="button" aria-pressed={!isRegister} onClick={() => switchMode("login")}>登录</button>
            <button type="button" aria-pressed={isRegister} onClick={() => switchMode("register")}>注册</button>
          </div>

          <header className="auth-heading">
            <h2 id={headingId}>{copy.title}</h2>
            <p>{copy.description}</p>
          </header>

          {mergedError ? <div className="auth-message is-error" role="alert">{mergedError}</div> : null}
          {notice ? <div className="auth-message is-info" role="status">{notice}</div> : null}

          <div className="auth-field">
            <label htmlFor={`${idPrefix}-username`}>用户名</label>
            <input
              id={`${idPrefix}-username`}
              className="auth-input"
              type="text"
              value={username}
              maxLength={20}
              autoComplete="username"
              aria-invalid={Boolean(fieldErrors.username)}
              aria-describedby={describedBy("username", isRegister ? `${idPrefix}-username-hint` : undefined)}
              onChange={(event) => { setUsername(event.target.value); clearFieldError("username"); }}
              disabled={loading}
            />
            {fieldError("username")}
            {isRegister ? <span id={`${idPrefix}-username-hint`} className="auth-hint">4–20 个字符，登录时使用。</span> : null}
          </div>

          {isRegister ? (
            <div className="auth-field">
              <label htmlFor={`${idPrefix}-display-name`}>显示名称 / 单位<span className="auth-optional">选填</span></label>
              <input
                id={`${idPrefix}-display-name`}
                className="auth-input"
                type="text"
                value={displayName}
                maxLength={48}
                autoComplete="organization"
                onChange={(event) => setDisplayName(event.target.value)}
                disabled={loading}
              />
            </div>
          ) : null}

          <div className="auth-field">
            <div className="auth-field-head">
              <label htmlFor={`${idPrefix}-password`}>密码</label>
              {isRegister ? null : (
                <button type="button" className="auth-link" onClick={() => { setLocalError(""); setNotice("暂不支持自助找回密码，请联系管理员重置。"); }} disabled={loading}>
                  忘记密码？
                </button>
              )}
            </div>
            <div className="auth-input-wrap">
              <input
                id={`${idPrefix}-password`}
                className="auth-input"
                type={showPassword ? "text" : "password"}
                value={password}
                maxLength={64}
                autoComplete={isRegister ? "new-password" : "current-password"}
                aria-invalid={Boolean(fieldErrors.password)}
                aria-describedby={describedBy("password", isRegister ? `${idPrefix}-strength` : undefined)}
                onChange={(event) => { setPassword(event.target.value); clearFieldError("password"); }}
                disabled={loading}
              />
              <button
                type="button"
                className="auth-reveal"
                onClick={() => setShowPassword((current) => !current)}
                aria-label={showPassword ? "隐藏密码" : "显示密码"}
                title={showPassword ? "隐藏密码" : "显示密码"}
                disabled={loading}
              >
                <EyeIcon open={showPassword} />
              </button>
            </div>
            {fieldError("password")}
            {isRegister ? (
              <div className="auth-strength" id={`${idPrefix}-strength`}>
                <span className="auth-strength-bars" aria-hidden="true">
                  {[0, 1, 2].map((index) => (
                    <span key={index} className={index < passwordStrength.filled ? `is-${passwordStrength.level}` : ""} />
                  ))}
                </span>
                <span>至少 8 位，含字母和数字 · 强度{passwordStrength.label}</span>
              </div>
            ) : null}
          </div>

          {isRegister ? (
            <div className="auth-field">
              <label htmlFor={`${idPrefix}-confirm-password`}>确认密码</label>
              <div className="auth-input-wrap">
                <input
                  id={`${idPrefix}-confirm-password`}
                  className="auth-input"
                  type={showConfirmPassword ? "text" : "password"}
                  value={confirmPassword}
                  maxLength={64}
                  autoComplete="new-password"
                  aria-invalid={Boolean(fieldErrors.confirmPassword)}
                  aria-describedby={describedBy("confirmPassword")}
                  onChange={(event) => { setConfirmPassword(event.target.value); clearFieldError("confirmPassword"); }}
                  disabled={loading}
                />
                <button
                  type="button"
                  className="auth-reveal"
                  onClick={() => setShowConfirmPassword((current) => !current)}
                  aria-label={showConfirmPassword ? "隐藏确认密码" : "显示确认密码"}
                  title={showConfirmPassword ? "隐藏确认密码" : "显示确认密码"}
                  disabled={loading}
                >
                  <EyeIcon open={showConfirmPassword} />
                </button>
              </div>
              {fieldError("confirmPassword")}
            </div>
          ) : null}

          <button type="submit" className="btn-primary auth-submit" disabled={loading}>
            {loading ? (
              <span className="auth-submit-content">
                <span className="spinner" aria-hidden="true" />
                <span>{copy.pending}</span>
              </span>
            ) : copy.submit}
          </button>
        </form>
      </section>
    </div>
  );
}
