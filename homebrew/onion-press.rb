cask "onion-press" do
  version "2.1.7"
  sha256 "5a3d9cad627f0a7d3810ef75686b0fa2961850b7eb865b2053c733920cf8c09c"

  url "https://github.com/brewsterkahle/onion.press/releases/download/v#{version}/onion.press.dmg"
  name "Onion.Press"
  desc "Easy-to-install WordPress with Tor Onion Service"
  homepage "https://github.com/brewsterkahle/onion.press"

  livecheck do
    url :url
    strategy :github_latest
  end

  depends_on macos: ">= :ventura"

  app "Onion.Press.app"

  zap trash: [
    "~/.onion.press",
  ]

  caveats <<~EOS
    ⚠️  Security Notice:

    On first launch, macOS will show a security warning because this app
    is not code-signed with an Apple Developer certificate.

    To open Onion.Press:
    1. Try to open the app (you'll see a warning)
    2. Open System Settings → Privacy & Security
    3. Scroll down and click "Open Anyway"
    4. Click "Open" in the confirmation dialog

    Or use this terminal command after installation:
      xattr -cr /Applications/Onion.Press.app

    First launch will take 3-5 minutes to download container images (~1GB).

    Access your WordPress site through Tor Browser:
    - Download Tor Browser: https://www.torproject.org/download/
    - Your .onion address will appear in the menu bar
  EOS
end
