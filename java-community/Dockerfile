# Java 社区服务镜像：两段构建（Maven 里打包 → 精简 JRE 里运行）。
#
# 为什么用多阶段而不是"把源码和 Maven 一起塞进运行镜像"：
#   运行镜像里根本不需要 Maven、源码和 ~300MB 的本地仓库。
#   分阶段之后运行镜像只有 JRE + 一个 jar，镜像小一个数量级，攻击面也小。
#
# 构建上下文是 java-community/（见 docker-compose.yml）。

# ---------------------------------------------------------------- 构建阶段
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /app

# ★ 把阿里云镜像配置带进容器。不这么做的话，容器内 Maven 会去连 Maven Central ——
#   在国内外会很慢（宿主机上我配了 ~/.m2/settings.xml，但容器里没有那个文件）。
COPY docker-maven-settings.xml /root/.m2/settings.xml

# 先只拷 pom 再下依赖：改业务代码时这一层能命中缓存，重建从几分钟降到几秒
COPY pom.xml ./
RUN mvn -B -q dependency:go-offline

COPY src ./src
# 跳过测试：CI 里已经跑过单测了，镜像构建不该重复跑（也不该因为测试环境差异挡住发布）
RUN mvn -B -q package -DskipTests

# ---------------------------------------------------------------- 运行阶段
FROM eclipse-temurin:17-jre-alpine
WORKDIR /app

# alpine 自带 wget，用它做健康检查；不装 curl 少一个包
COPY --from=build /app/target/*.jar app.jar

# 时区：容器默认 UTC，而行程/点赞都是按本地日期算的，差 8 小时会出现在"今天"的判断上
ENV TZ=Asia/Shanghai
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/community/health || exit 1

# 让 JVM 在容器里正确感知内存上限（不然它按宿主机内存算堆，容易被 OOMKill）
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "/app/app.jar"]
