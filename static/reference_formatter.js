const form = document.getElementById("formatterForm");
const styleSelect = document.getElementById("style");
const variantWrap = document.getElementById("variantWrap");
const formattedOutput = document.getElementById("formattedOutput");
const copyBtn = document.getElementById("copyBtn");
const warningsBox = document.getElementById("warningsBox");
const warningsList = document.getElementById("warningsList");

function toggleVariant() {
    if (styleSelect.value === "harvard") {
        variantWrap.classList.remove("hidden");
    } else {
        variantWrap.classList.add("hidden");
    }
}

toggleVariant();
styleSelect.addEventListener("change", toggleVariant);

form.addEventListener("submit", async function (e) {
    e.preventDefault();

    formattedOutput.textContent = "Formatting...";
    warningsList.innerHTML = "";
    warningsBox.classList.add("hidden");

    const formData = new FormData(form);

    try {
        const response = await fetch("/api/format-reference", {
            method: "POST",
            body: formData
        });

        const data = await response.json();

        if (!data.success) {
            formattedOutput.textContent = data.message || "Formatting failed.";
            return;
        }

        formattedOutput.textContent = data.formatted || "";

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
        formattedOutput.textContent = "An error occurred while formatting the reference(s).";
    }
});

copyBtn.addEventListener("click", async function () {
    const text = formattedOutput.textContent.trim();

    if (!text || text === "Your formatted reference(s) will appear here.") {
        return;
    }

    try {
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = "Copied";
        setTimeout(() => {
            copyBtn.textContent = "Copy Output";
        }, 1200);
    } catch (error) {
        copyBtn.textContent = "Copy failed";
        setTimeout(() => {
            copyBtn.textContent = "Copy Output";
        }, 1200);
    }
});
