{
  description = "Cognimoss local development environment";

  inputs = {
    # flake.lock will pin the exact revision for reproducible development.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { nixpkgs, ... }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;

      mkPkgs =
        system:
        import nixpkgs {
          inherit system;

          config = {
            # Required by the CUDA package set used by ollama-cuda.
            allowUnfree = true;

            # RTX 4070 Ti / Ada Lovelace.
            cudaCapabilities =
              if system == "x86_64-linux" then
                [ "8.9" ]
              else
                [ ];
          };
        };
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = mkPkgs system;
          lib = pkgs.lib;

          python = pkgs.python311.withPackages (
            ps: with ps; [
              pip
              setuptools
              wheel
              virtualenv
              pytest
            ]
          );

          commonPackages =
            (with pkgs; [
              # Python/runtime
              python

              # Git and GitHub operations
              git
              git-lfs
              gh
              openssh

              # Container workflow
              docker
              docker-compose

              # AWS/Moto inspection
              awscli2

              # HTTP, JSON, YAML, and debugging
              curl
              wget
              jq
              yq-go
              ripgrep
              fd
              tree
              lsof
              iproute2

              # Native Python package build dependencies
              gcc
              gnumake
              cmake
              ninja
              pkg-config
              patchelf

              # Some Python dependencies build Rust extensions
              rustc
              cargo

              # Common native libraries used by Python wheels
              openssl
              libffi
              zlib
              sqlite
              libxml2
              libxslt
              libjpeg
              libpng

              # Project/script quality tools
              ruff
              shellcheck
              shfmt
              yamllint

              # Nix formatting
              nixfmt-rfc-style
            ]);

          mkCognimossShell =
            ollamaPackage:
            pkgs.mkShell {
              name = "cognimoss-dev";

              packages = commonPackages ++ [ ollamaPackage ];

              # Helps pip-installed native extensions find common Nix libraries.
              LD_LIBRARY_PATH = lib.makeLibraryPath [
                pkgs.stdenv.cc.cc.lib
                pkgs.openssl
                pkgs.libffi
                pkgs.zlib
                pkgs.sqlite
                pkgs.libxml2
                pkgs.libxslt
                pkgs.libjpeg
                pkgs.libpng
              ];

              shellHook = ''
                export PYTHONPATH="$PWD/cognimoss-core:$PWD/backend_package:$PWD/frontend_package''${PYTHONPATH:+:$PYTHONPATH}"

                export COGNIMOSS_COMPOSE_FILE="$PWD/docker-compose.local.yml"

                # Keep large Ollama models out of the Nix store and inside the
                # ignored local development directory.
                export OLLAMA_MODELS="''${OLLAMA_MODELS:-$PWD/.local/ollama-models}"

                export PIP_DISABLE_PIP_VERSION_CHECK=1
                export DOCKER_BUILDKIT=1
                export COMPOSE_DOCKER_CLI_BUILD=1

                mkdir -p "$PWD/.local" "$OLLAMA_MODELS"

                _cognimoss_compose() {
                  if [ ! -f "$PWD/.env.local" ]; then
                    echo "ERROR: .env.local does not exist."
                    echo "Create it with:"
                    echo "  cp .env.local.example .env.local"
                    return 1
                  fi

                  docker compose \
                    --env-file "$PWD/.env.local" \
                    -f "$COGNIMOSS_COMPOSE_FILE" \
                    "$@"
                }

                cognimoss-up() {
                  _cognimoss_compose up -d --build
                }

                cognimoss-down() {
                  _cognimoss_compose down
                }

                cognimoss-reset() {
                  _cognimoss_compose down -v
                }

                cognimoss-ps() {
                  _cognimoss_compose ps -a
                }

                cognimoss-logs() {
                  _cognimoss_compose logs -f "$@"
                }

                cognimoss-check() {
                  if [ -x "$PWD/scripts/local-check.sh" ]; then
                    "$PWD/scripts/local-check.sh"
                  else
                    echo "scripts/local-check.sh is missing or not executable."
                    return 1
                  fi
                }

                # Bind Ollama so Linux Docker containers can reach the host
                # through host.docker.internal.
                cognimoss-ollama() {
                  echo "Starting Ollama on 0.0.0.0:11434..."
                  echo "Ensure your firewall does not expose port 11434 publicly."
                  OLLAMA_HOST=0.0.0.0:11434 ollama serve
                }

                cognimoss-models() {
                  OLLAMA_HOST=127.0.0.1:11434 \
                    ollama pull qwen2.5-coder:14b

                  OLLAMA_HOST=127.0.0.1:11434 \
                    ollama pull nomic-embed-text
                }

                # Optional direct-host Python environment. Docker remains the
                # authoritative full-stack runtime.
                cognimoss-venv() {
                  if [ ! -d "$PWD/.venv" ]; then
                    python -m venv "$PWD/.venv"
                  fi

                  source "$PWD/.venv/bin/activate"

                  python -m pip install --upgrade \
                    pip setuptools wheel

                  python -m pip install \
                    -r frontend_package/requirements.txt \
                    -r backend_package/requirements.txt
                }

                echo
                echo "Cognimoss development shell"
                echo "  Python: $(python --version 2>&1)"
                echo "  Ollama: $(ollama --version 2>&1 || true)"
                echo
                echo "Commands:"
                echo "  cognimoss-ollama   Start host Ollama for Docker"
                echo "  cognimoss-models   Pull the required Ollama models"
                echo "  cognimoss-up       Build and start the local stack"
                echo "  cognimoss-ps       Show container states"
                echo "  cognimoss-logs     Follow all logs"
                echo "  cognimoss-logs frontend"
                echo "  cognimoss-check    Run local verification"
                echo "  cognimoss-down     Stop the stack"
                echo "  cognimoss-reset    Stop and delete local volumes"
                echo "  cognimoss-venv     Build an optional host Python venv"
                echo
              '';
            };
        in
        {
          # Portable CPU Ollama shell.
          default = mkCognimossShell pkgs.ollama;

          # NVIDIA CUDA shell. On non-x86 Linux, fall back to CPU Ollama.
          cuda = mkCognimossShell (
            if system == "x86_64-linux" then
              pkgs.ollama-cuda
            else
              pkgs.ollama
          );
        }
      );

      formatter = forAllSystems (
        system: (mkPkgs system).nixfmt-rfc-style
      );
    };
}
