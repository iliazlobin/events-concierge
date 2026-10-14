import { LoaderCircle } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

type SaveButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children" | "className"> & {
  saving: boolean;
};

export function SettingsSaveButton({ saving, disabled, type = "button", ...props }: SaveButtonProps) {
  return (
    <button
      {...props}
      type={type}
      className="button button-primary settings-save-button"
      disabled={disabled || saving}
      aria-busy={saving}
    >
      {saving ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
      Save
    </button>
  );
}

export function SettingsActions({
  status,
  leading,
  children,
}: {
  status: ReactNode;
  leading?: ReactNode;
  children: ReactNode;
}) {
  return (
    <footer className="settings-actions">
      <div className="settings-actions__context">
        {leading}
        <span className="settings-actions__state" role="status">{status}</span>
      </div>
      <div className="settings-actions__buttons">{children}</div>
    </footer>
  );
}
