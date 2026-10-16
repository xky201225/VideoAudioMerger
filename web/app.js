const videoInput = document.getElementById("videoInput");
const audioInput = document.getElementById("audioInput");
const videoZone = document.getElementById("videoZone");
const audioZone = document.getElementById("audioZone");
const videoMeta = document.getElementById("videoMeta");
const audioMeta = document.getElementById("audioMeta");
const formatSelect = document.getElementById("formatSelect");
const mergeForm = document.getElementById("mergeForm");
const mergeBtn = document.getElementById("mergeBtn");
const progressPanel = document.getElementById("progressPanel");
const progressStatus = document.getElementById("progressStatus");
const progressPercent = document.getElementById("progressPercent");
const progressBar = document.getElementById("progressBar");
const resultPanel = document.getElementById("resultPanel");
const resultName = document.getElementById("resultName");
const downloadLink = document.getElementById("downloadLink");
const engineStatus = document.getElementById("engineStatus");

let selectedVideo = null;
let selectedAudio = null;
let pollingTimer = null;

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / Math.pow(1024, index);
  return `${value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

function setVideo(file) {
  selectedVideo = file;
  videoMeta.textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "未选择";
  videoZone.classList.toggle("has-file", Boolean(file));
  updateMergeButton();
}

function setAudio(file) {
  selectedAudio = file;
  audioMeta.textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "未选择";
  audioZone.classList.toggle("has-file", Boolean(file));
  updateMergeButton();
}

function updateMergeButton() {
  mergeBtn.disabled = !(selectedVideo && selectedAudio) || mergeBtn.dataset.busy === "true";
}

function bindDrop(zone, setter) {
  ["dragenter", "dragover"].forEach((eventName) => {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.remove("dragover");
    });
  });
  zone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) setter(file);
  });
}

videoInput.addEventListener("change", () => setVideo(videoInput.files[0] || null));
audioInput.addEventListener("change", () => setAudio(audioInput.files[0] || null));
bindDrop(videoZone, setVideo);
bindDrop(audioZone, setAudio);

function resetPanels() {
  resultPanel.hidden = true;
  progressPanel.hidden = true;
  progressBar.style.width = "0%";
  progressPercent.textContent = "0%";
  progressStatus.textContent = "准备中";
  progressStatus.classList.remove("error-text");
}

async function checkHealth() {
  try {
    const response = await fetch("/api/health");
    const data = await response.json();
    if (data.ok) {
      engineStatus.textContent = data.ffmpeg || "FFmpeg 已就绪";
      return;
    }
  } catch (_error) {
    // Ignore network errors here; the merge flow will surface a clearer message.
  }
  engineStatus.textContent = "FFmpeg 状态未知";
}

function stopPolling() {
  if (pollingTimer) {
    clearTimeout(pollingTimer);
    pollingTimer = null;
  }
}

async function pollProgress(jobId) {
  stopPolling();
  try {
    const response = await fetch(`/api/progress?job=${encodeURIComponent(jobId)}`);
    const data = await response.json();
    if (!data.ok || !data.job) {
      throw new Error(data.error || "无法读取任务状态");
    }
    const job = data.job;
    const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
    progressBar.style.width = `${progress}%`;
    progressPercent.textContent = `${Math.round(progress)}%`;
    progressStatus.textContent = job.message || "处理中";

    if (job.status === "done") {
      progressStatus.textContent = job.message || "合并完成";
      resultName.textContent = job.output_name || "merged";
      downloadLink.href = `/download?job=${encodeURIComponent(jobId)}`;
      resultPanel.hidden = false;
      mergeBtn.dataset.busy = "false";
      mergeBtn.disabled = false;
      mergeBtn.innerHTML = mergeBtn.dataset.idleHtml;
      stopPolling();
      return;
    }

    if (job.status === "error") {
      progressStatus.textContent = job.message || "处理失败";
      progressStatus.classList.add("error-text");
      mergeBtn.dataset.busy = "false";
      mergeBtn.disabled = false;
      mergeBtn.innerHTML = mergeBtn.dataset.idleHtml;
      updateMergeButton();
      stopPolling();
      return;
    }

    pollingTimer = setTimeout(() => pollProgress(jobId), 700);
  } catch (error) {
    progressStatus.textContent = error.message || "进度查询失败";
    progressStatus.classList.add("error-text");
    mergeBtn.dataset.busy = "false";
    mergeBtn.disabled = false;
    mergeBtn.innerHTML = mergeBtn.dataset.idleHtml;
    updateMergeButton();
    stopPolling();
  }
}

mergeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedVideo || !selectedAudio) return;

  resetPanels();
  progressPanel.hidden = false;
  mergeBtn.dataset.busy = "true";
  mergeBtn.dataset.idleHtml = mergeBtn.innerHTML;
  mergeBtn.disabled = true;
  mergeBtn.textContent = "正在上传...";
  progressStatus.textContent = "正在上传文件";
  progressPercent.textContent = "0%";

  const formData = new FormData();
  formData.append("video", selectedVideo);
  formData.append("audio", selectedAudio);
  formData.append("format", formatSelect.value);

  try {
    const response = await fetch("/merge", {
      method: "POST",
      body: formData,
    });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.error || "上传失败");
    }
    pollProgress(data.job);
  } catch (error) {
    progressStatus.textContent = error.message || "上传失败，请重试";
    progressStatus.classList.add("error-text");
    mergeBtn.dataset.busy = "false";
    mergeBtn.disabled = false;
    mergeBtn.innerHTML = mergeBtn.dataset.idleHtml;
    updateMergeButton();
  }
});

checkHealth();
updateMergeButton();
