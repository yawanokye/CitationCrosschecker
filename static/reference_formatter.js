const form = document.getElementById("formatterForm");
const styleSelect = document.getElementById("style");
const formattedOutput = document.getElementById("formattedOutput");
const copyBtn = document.getElementById("copyBtn");
const warningsBox = document.getElementById("warningsBox");
const warningsList = document.getElementById("warningsList");
const formatBtn = document.getElementById("formatBtn");

if (!form) {
    console.error("formatterForm not found");
}

form.addEventListener("submit", async function (e) {
    e.preventDefault();

    const rawReference = document.getElementById("raw_reference").value.trim();

    if (!rawReference) {
        formattedOutput.textContent = "Please paste at least one reference.";
        warningsList.innerHTML = "";
        warningsBox.classList.add("hidden");
        return;
    }

    formattedOutput.textContent = "Formatting...";
    formattedOutput.classList.add("loading");
    formatBtn.disabled = true;
    warningsList.innerHTML = "";
    warningsBox.classList.add("hidden");

    const formData = new FormData(form);

    try {
        const response = await fetch("/api/format-reference", {
            method: "POST",
            body: formData
        });

        const data = await response.json();

        if (!response.ok || !data.success) {
            formattedOutput.textContent = data.message || "Formatting failed.";
            return;
        }

        formattedOutput.textContent = data.formatted || "No output returned.";

        if (data.warnings && data.warnings.length > 0) {
            warningsList.innerHTML = "";
            data.warnings.forEach(function (warning) {
                const li = document.createElement("li");
                li.textContent = warning;
                warningsList.appendChild(li);
            });
            warningsBox.classList.remove("hidden");
        }
    } catch (error) {
        console.error(error);
        formattedOutput.textContent = "An error occurred while formatting the reference(s).";
    } finally {
        formattedOutput.classList.remove("loading");
        formatBtn.disabled = false;
    }
});

copyBtn.addEventListener("click", async function () {
    const text = formattedOutput.textContent.trim();

    if (!text || text === "Your formatted references will appear here.") {
        return;
    }

    try {
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = "Copied";
        setTimeout(() => {
            copyBtn.textContent = "Copy";
        }, 1200);
    } catch (error) {
        console.error(error);
        copyBtn.textContent = "Copy failed";
        setTimeout(() => {
            copyBtn.textContent = "Copy";
        }, 1200);
    }
});
