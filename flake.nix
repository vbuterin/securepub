{
  description = "securepub: host-approved exact-commit GitHub publication";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/e158d9ed9b51c98974c5e66e1ba1c9e0255fecaa";
  inputs.monkeybox.url = "github:vbuterin/monkeybox/5c6e50a406e32a8ea6e7ea380960d24b88fdfaf4";
  inputs.monkeybox.inputs.nixpkgs.follows = "nixpkgs";
  outputs = { self, nixpkgs, monkeybox }: let
    systems = [ "x86_64-linux" "aarch64-linux" ];
    each = nixpkgs.lib.genAttrs systems;
  in {
    packages = each (system: let
      pkgs = import nixpkgs { inherit system; };
      exporter = monkeybox.lib.mkMonkeybox {
        inherit pkgs;
        settings = {
          networkMode = "blocked";
          memory = "2048M";
          shareHostProfile = false;
          packages = [ pkgs.git pkgs.python3 ];
          persist = [];
          bind = {};
          # mbox expands this trusted, sanitized per-invocation host variable.
          bindReadOnly."$SECUREPUB_CHECKOUT".enable = true;
          bashrc = null;
          shellHook = "";
        };
      };
    in {
      default = pkgs.stdenvNoCC.mkDerivation {
        pname = "securepub";
        version = "0.2.0-beta";
        src = self;
        nativeBuildInputs = [ pkgs.python3 pkgs.git pkgs.makeWrapper ];
        dontConfigure = true;
        dontBuild = true;
        doCheck = true;
        checkPhase = ''
          export HOME="$TMPDIR/test-home"
          mkdir -p "$HOME"
          PYTHONDONTWRITEBYTECODE=1 python3 tests/test_securepub.py -v
        '';
        installPhase = ''
          mkdir -p "$out/lib/securepub" "$out/bin"
          cp securepub.py export_checkout.py "$out/lib/securepub/"
          substituteInPlace "$out/lib/securepub/securepub.py" \
            --replace-fail 'GIT_EXECUTABLE = "git"' 'GIT_EXECUTABLE = "${pkgs.git}/bin/git"' \
            --replace-fail 'SSH_EXECUTABLE = "ssh"' 'SSH_EXECUTABLE = "${pkgs.openssh}/bin/ssh"' \
            --replace-fail 'BWRAP_EXECUTABLE = "bwrap"' 'BWRAP_EXECUTABLE = "${pkgs.bubblewrap}/bin/bwrap"' \
            --replace-fail 'MONKEYBOX_EXECUTABLE = "@securepub-monkeybox@"' 'MONKEYBOX_EXECUTABLE = "${exporter.wrapper}/bin/mbox"'
          makeWrapper ${pkgs.python3}/bin/python3 "$out/bin/securepub" \
            --add-flags "-I $out/lib/securepub/securepub.py"
        '';
        meta = {
          description = "User-gated publication of untrusted Git proposals";
          platforms = systems;
          mainProgram = "securepub";
        };
      };
    });
    apps = each (system: {
      default = { type = "app"; program = "${self.packages.${system}.default}/bin/securepub"; };
    });
    checks = each (system: { package = self.packages.${system}.default; });
    nixosModules.default = { config, lib, pkgs, ... }: {
      options.programs.securepub.enable = lib.mkEnableOption "host-only gated GitHub publisher";
      config = lib.mkIf config.programs.securepub.enable {
        environment.systemPackages = [ self.packages.${pkgs.stdenv.hostPlatform.system}.default ];
      };
    };
  };
}
