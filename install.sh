#!/bin/sh
set -eu

OFFBOX_SOURCE="${OFFBOX_SOURCE:-git+https://github.com/Avdeev99/offbox}"

say() {
  printf '%s\n' "$*" >&2
}

fail() {
  say "error: $1"
  [ $# -lt 2 ] || say "fix: $2"
  exit 1
}

case "$(uname -s)" in
  Darwin | Linux) ;;
  MINGW* | MSYS* | CYGWIN*)
    fail "Git Bash and other native Windows shells are not supported." \
      "Install WSL2 with 'wsl --install', open Ubuntu and run this installer there."
    ;;
  *) fail "Unsupported operating system: $(uname -s)." "Use macOS, Linux or Windows through WSL2." ;;
esac

for tool in git ssh; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    if [ "$(uname -s)" = Darwin ]; then
      fail "$tool is not installed." "Run 'xcode-select --install', then rerun this installer."
    fi
    fail "$tool is not installed." "Install it with your package manager, e.g. 'sudo apt install git openssh-client', then rerun this installer."
  fi
done

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (https://docs.astral.sh/uv/)..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    fail "Neither curl nor wget is available to install uv." "Install curl, then rerun this installer."
  fi
  if [ -f "$HOME/.local/bin/env" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.local/bin/env"
  fi
  PATH="${UV_INSTALL_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}:$PATH"
  export PATH
  command -v uv >/dev/null 2>&1 || fail "uv was installed but is not on PATH." "Open a new terminal, then rerun this installer."
fi

say "Installing offbox from $OFFBOX_SOURCE..."
uv tool install --force "$OFFBOX_SOURCE"

bin_dir="$(uv tool dir --bin)"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    if uv tool update-shell >/dev/null 2>&1; then
      say "Added $bin_dir to your shell profile; open a new terminal to use offbox there."
    else
      say "Add $bin_dir to PATH in your shell profile:"
      say "  export PATH=\"$bin_dir:\$PATH\""
    fi
    PATH="$bin_dir:$PATH"
    export PATH
    ;;
esac

if [ -t 2 ] && (exec </dev/tty) 2>/dev/null; then
  say "Running offbox setup..."
  if ! offbox setup </dev/tty; then
    say "offbox is installed; setup did not finish. Run 'offbox setup' again."
  fi
else
  say "Installed. Next: run 'offbox setup'."
fi
