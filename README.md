# TTFHW-sglang

sglang 自动化编译、镜像制作仓库。

## 项目简介

本仓库用于自动化构建 sglang NPU 版本的 Docker 镜像，支持多架构（amd64/arm64）构建，并推送到 GitHub Container Registry (ghcr.io)。

## 源码引用

- **sglang 主社区仓库**: https://github.com/sgl-project/sglang.git
- **NPU Dockerfile 路径**: `sglang/docker/npu.Dockerfile`

## 构建参数

镜像构建支持以下参数配置：

| 参数名 | 说明 | 配置方式 | 示例值 |
|--------|------|----------|--------|
| `image_tag` | 镜像标签后缀 | 手动输入 | `B001` |
| `CANN_VERSION` | CANN（Compute Architecture for Neural Networks）版本号 | 固定值 | `8.5.0` |
| `DEVICE_TYPE` | 目标设备类型 | Matrix 自动构建 | `a3`, `910b` |
| `SGLANG_KERNEL_NPU_TAG` | NPU Kernel 镜像标签 | 固定值 | `2026.04.15.rc2` |

## 工作流程

本仓库使用 GitHub Actions 自动化构建流程：

1. **拉取源码**: 从 sglang 主社区仓库拉取最新代码
2. **设置 QEMU**: 使用 `docker/setup-qemu-action@v3` 支持多架构构建
3. **构建镜像**: 使用 `docker/build-push-action@v6` 构建并推送多架构镜像
4. **参数传递**: 
   - `image_tag` 通过手动输入
   - `CANN_VERSION` 和 `DEVICE_TYPE` 通过 matrix 策略自动构建所有组合
   - `SGLANG_KERNEL_NPU_TAG` 为固定值 `2026.04.15.rc2`

### 前置条件

1. 确保仓库已启用 GitHub Actions
2. 确保仓库已启用 GitHub Packages（在 Settings -> Packages 中）

### 设置镜像为公开（Public）

默认情况下，推送到 ghcr.io 的镜像为私有（Private）类型。如需将镜像设置为公开类型，让所有人都可以无需认证直接拉取：

1. 构建完成后，进入 GitHub 仓库页面
2. 点击 "Packages" 标签
3. 点击 `sglang_npu` 镜像包
4. 点击右侧 "Package Settings"
5. 在 "Visibility" 区域，点击 "Change visibility"
6. 选择 "Public" 并确认

> 注意：公开镜像后，任何人都可以无需认证直接拉取镜像，无需使用 `docker login` 命令。

### 手动触发构建

1. 进入 Actions 页面
2. 选择 "Build NPU Image" 工作流
3. 点击 "Run workflow"
4. 输入镜像标签后缀（例如：`B001`）
5. 点击 "Run workflow" 开始构建

> 注意：本工作流仅支持手动触发，无自动触发。

### 权限配置

Workflow 需要以下权限：
- `contents: read` - 读取仓库代码
- `packages: write` - 推送镜像到 ghcr.io

这些权限已在 workflow 文件中配置，无需额外设置。

## 输出镜像

### 镜像仓库

构建完成的镜像将推送到 GitHub Container Registry：

```
ghcr.io/<owner>/sglang_npu:<CANN_VERSION>-<DEVICE_TYPE>-<image_tag>
```

### 镜像标签格式

| 参数 | 说明 |
|------|------|
| `CANN_VERSION` | 固定值 `8.5.0` |
| `DEVICE_TYPE` | `a3`, `910b` |
| `image_tag` | 手动输入的标签后缀 |

示例标签：
- `8.5.0-a3-B001`
- `8.5.0-910b-B001`

### 查看已推送的镜像

1. 进入仓库页面
2. 点击 "Packages" 标签
3. 选择对应的镜像包查看详细信息和拉取命令

### 拉取镜像

```bash
# 拉取特定版本（公开镜像，无需认证）
docker pull ghcr.io/<owner>/sglang_npu:8.5.0-a3-B001
docker pull ghcr.io/<owner>/sglang_npu:8.5.0-910b-B001
```

> 注意：如果镜像已设置为公开（Public）类型，拉取时无需认证。如果是私有镜像，需要先登录 ghcr.io。

## Matrix 构建组合

每次手动触发将自动构建以下组合：

| CANN_VERSION | DEVICE_TYPE | SGLANG_KERNEL_NPU_TAG |
|--------------|-------------|----------------------|
| 8.5.0 | a3 | 2026.04.15.rc2 |
| 8.5.0 | 910b | 2026.04.15.rc2 |

## 许可证

遵循 sglang 主社区许可证。
