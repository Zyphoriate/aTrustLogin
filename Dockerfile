FROM hagb/docker-atrust:latest
LABEL authors="Kenvix"

ENV TZ="Asia/Shanghai"
COPY ./docker/bin /bin
COPY ./src /opt/atrust-autologin

# 再次定义 ARG 变量以确保构建过程中可以使用这些参数
ARG ANDROID_PATCH
ARG EC_HOST
ARG VPN_TYPE=EC_GUI
ARG VPN_URL
ARG ELECTRON_URL
ARG USE_VPN_ELECTRON
ARG VPN_DEB_PATH

# 重新定义 ENV 变量，以确保环境变量在最终镜像中可用
ENV PING_INTERVAL=1800

# 保留基础镜像的卷（这会继承 `/root` 和 `/usr/share/sangfor/EasyConnect/resources/logs/` 的设置）
VOLUME ["/root", "/usr/share/sangfor/EasyConnect/resources/logs/"]

# 使用 Debian 官方源与官方 PyPI。
# 基础镜像预置的是国内镜像源（ftp.cn.debian.org），此前的写法又在其上叠加了
# mirrors.ustc.edu.cn，两个镜像的索引与 pool 不同步时会出现 404
# （例如 libxnvctrl0_535.309.01-0+deb11u1，索引里是 bullseye 的包名、pool 里没有）。
# CI 跑在 GitHub 的境外 runner 上，走官方源更快也更稳。
# 下面这份源列表与基础镜像自身的 build-scripts/config-apt.sh 保持一致，只换了地址；
# 如确实要在国内构建，把两个 URL 换回镜像站即可。
RUN echo "Begin build" && \
    . /etc/os-release && \
    printf '%s\n' \
        "deb http://deb.debian.org/debian trixie main" \
        "deb http://deb.debian.org/debian ${VERSION_CODENAME} main" \
        "deb http://deb.debian.org/debian-security ${VERSION_CODENAME}-security main" \
        "deb http://deb.debian.org/debian bullseye main" \
        "deb http://deb.debian.org/debian-security bullseye-security main" \
        > /etc/apt/sources.list && \
    rm -f /root/.pip/pip.conf && \
    date > /etc/build-date.txt && \
    chmod +x /bin/start-with-autologin.sh && \
    chmod +x /bin/start-with-autologin-actual.sh && \
    chmod +x /bin/start-port-forwarding.sh && \
    apt-get update && \
    apt-get install -y --no-install-recommends --no-install-suggests apt-utils curl x11-xserver-utils && \
    apt-get install -y --no-install-recommends --no-install-suggests chromium chromium-driver chromium-l10n python3 python3-pip && \
    cd /opt/atrust-autologin && \
    pip3 install --break-system-packages -r requirements.txt && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

CMD ["/bin/start-with-autologin.sh"]