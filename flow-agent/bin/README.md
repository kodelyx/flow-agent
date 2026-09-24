# Flow Engine Binary

This directory contains the pre-compiled binary (`flow`) that handles the low-level communication with Google Flow.

---

## 🔗 Original Source Repository

The complete Go source code, build scripts, and tests for this binary are maintained in:

👉 **[https://github.com/kodelyx/flow-go](https://github.com/kodelyx/flow-go)**

---

## 🛠️ How to Modify & Recompile

If you need to edit or recompile the binary:

### 1. Clone the Source Repository
```bash
git clone https://github.com/kodelyx/flow-go.git
cd flow-go/flow-go
```

### 2. Make Your Changes
- Internal logic and RPCs are located in `internal/`.
- CLI commands are in `cmd/` or the root `main.go`.

### 3. Compile the Binary
```bash
# Build the binary
make build
```

### 4. Copy the New Binary to Flow Agent
```bash
cp flow /path/to/flow-agent/bin/flow
```

---

## ⚡ Binary Subcommands Reference

```bash
./bin/flow bridge         # Start WebSocket listener for Chrome extension
./bin/flow image "..."    # Direct image generation
./bin/flow generate "..." # Direct video generation
./bin/flow balance        # Show credit balances
./bin/flow stats          # Show generation analytics
./bin/flow cookies        # Inspect stored account cookies
./bin/flow version        # Check engine version
```
