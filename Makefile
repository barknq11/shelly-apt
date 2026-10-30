PREFIX ?= /usr
DESTDIR ?=
LIBDIR = $(DESTDIR)$(PREFIX)/lib/shelly-apt
BINDIR = $(DESTDIR)$(PREFIX)/bin
SHAREDIR = $(DESTDIR)$(PREFIX)/share

.PHONY: all install uninstall check deb clean

all:
	python3 -m compileall -q shelly_apt

check:
	python3 -m py_compile shelly_apt/*.py
	./bin/shelly --version
	./bin/shelly utility completions bash | bash -n

install:
	install -d $(LIBDIR)/shelly_apt $(BINDIR)
	install -m 644 shelly_apt/*.py $(LIBDIR)/shelly_apt/
	install -m 755 bin/shelly bin/shelly-ui bin/shelly-notifications $(BINDIR)/
	install -Dm 644 data/org.shellyapt.Shelly.desktop $(SHAREDIR)/applications/org.shellyapt.Shelly.desktop
	install -Dm 644 data/org.shellyapt.Shelly.policy $(SHAREDIR)/polkit-1/actions/org.shellyapt.Shelly.policy
	install -Dm 644 data/icons/hicolor/scalable/apps/org.shellyapt.Shelly.svg $(SHAREDIR)/icons/hicolor/scalable/apps/org.shellyapt.Shelly.svg
	install -Dm 644 data/shelly-notifications.service $(DESTDIR)$(PREFIX)/lib/systemd/user/shelly-notifications.service
	install -Dm 644 data/shelly-notifications.timer $(DESTDIR)$(PREFIX)/lib/systemd/user/shelly-notifications.timer
	install -d $(SHAREDIR)/bash-completion/completions $(SHAREDIR)/zsh/vendor-completions $(SHAREDIR)/fish/vendor_completions.d
	./bin/shelly utility completions bash > $(SHAREDIR)/bash-completion/completions/shelly
	./bin/shelly utility completions zsh > $(SHAREDIR)/zsh/vendor-completions/_shelly
	./bin/shelly utility completions fish > $(SHAREDIR)/fish/vendor_completions.d/shelly.fish

uninstall:
	rm -rf $(LIBDIR)
	rm -f $(BINDIR)/shelly $(BINDIR)/shelly-ui $(BINDIR)/shelly-notifications
	rm -f $(SHAREDIR)/applications/org.shellyapt.Shelly.desktop $(SHAREDIR)/polkit-1/actions/org.shellyapt.Shelly.policy
	rm -f $(SHAREDIR)/icons/hicolor/scalable/apps/org.shellyapt.Shelly.svg
	rm -f $(DESTDIR)$(PREFIX)/lib/systemd/user/shelly-notifications.service $(DESTDIR)$(PREFIX)/lib/systemd/user/shelly-notifications.timer
	rm -f $(SHAREDIR)/bash-completion/completions/shelly $(SHAREDIR)/zsh/vendor-completions/_shelly $(SHAREDIR)/fish/vendor_completions.d/shelly.fish

deb:
	dpkg-buildpackage -us -uc -b

clean:
	-find . -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
