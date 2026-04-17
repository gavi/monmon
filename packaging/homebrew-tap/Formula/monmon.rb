class Monmon < Formula
  include Language::Python::Virtualenv

  desc "macOS silicon monitor (E/P cores, GPU, NPU) with a TUI"
  homepage "https://github.com/gavi/monmon"
  # After `uv build && uv publish`, replace the url + sha256 with the PyPI sdist.
  # Example:
  #   url "https://files.pythonhosted.org/packages/source/m/monmon/monmon-0.1.0.tar.gz"
  # You can find the URL + sha256 on the PyPI project page under "Download files".
  url "https://files.pythonhosted.org/packages/source/m/monmon/monmon-0.1.0.tar.gz"
  sha256 "REPLACE_WITH_SDIST_SHA256"
  license "MIT"

  depends_on "python@3.12"

  # The `resource` stanzas below are placeholders. After editing this formula,
  # run:
  #   brew update-python-resources monmon
  # It will rewrite these blocks with real PyPI URLs + sha256s for every
  # transitive dependency of monmon.
  resource "psutil" do
    url "https://files.pythonhosted.org/packages/source/p/psutil/psutil-7.2.2.tar.gz"
    sha256 "REPLACE"
  end

  resource "textual" do
    url "https://files.pythonhosted.org/packages/source/t/textual/textual-8.2.3.tar.gz"
    sha256 "REPLACE"
  end

  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      monmon reads Apple's powermetrics, which requires root privileges.
      You will be prompted for your password the first time you run it.

      If the in-process sudo prompt fails (common on TouchID-only setups),
      cache your credential in the shell first:

        sudo -v && monmon
    EOS
  end

  test do
    assert_match "monmon", shell_output("#{bin}/monmon --help")
  end
end
