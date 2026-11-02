const onboardingEmailForm = document.getElementById("onboardingEmailForm");
if (onboardingEmailForm) {
    const onboardingEmailInput = document.getElementById("onboardingStudentEmail");
    const onboardingEmailButton = document.getElementById("onboardingEmailSend");
    const onboardingEmailStatus = document.getElementById("onboardingEmailStatus");

    onboardingEmailForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const recipient = onboardingEmailInput.value.trim();

        if (!recipient) {
            onboardingEmailInput.setAttribute("aria-invalid", "true");
            onboardingEmailStatus.className = "onboarding-email-status is-error";
            onboardingEmailStatus.textContent = "Please enter the student's email address.";
            onboardingEmailInput.focus();
            return;
        }

        if (!onboardingEmailInput.checkValidity()) {
            onboardingEmailInput.setAttribute("aria-invalid", "true");
            onboardingEmailStatus.className = "onboarding-email-status is-error";
            onboardingEmailStatus.textContent = "Please enter a valid email address.";
            onboardingEmailInput.focus();
            return;
        }

        onboardingEmailInput.setAttribute("aria-invalid", "false");
        onboardingEmailStatus.className = "onboarding-email-status";
        onboardingEmailStatus.textContent = "";
        onboardingEmailButton.disabled = true;
        onboardingEmailButton.classList.add("is-loading");
        onboardingEmailButton.setAttribute("aria-busy", "true");

        try {
            const response = await fetch("/api/onboarding-email", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ student_email: recipient }),
            });
            let result;
            try {
                result = await response.json();
            } catch {
                throw new Error("The server returned an invalid response.");
            }

            if (result.email_sent && !result.sent_copy_saved) {
                onboardingEmailStatus.className = "onboarding-email-status is-warning";
                onboardingEmailStatus.textContent = result.error;
                return;
            }

            if (!response.ok || !result.success) {
                onboardingEmailStatus.className = "onboarding-email-status is-error";
                onboardingEmailStatus.textContent = result.error || "Onboarding email delivery failed.";
                return;
            }

            onboardingEmailStatus.className = "onboarding-email-status is-success";
            onboardingEmailStatus.textContent = result.message || "Onboarding email sent successfully.";
        } catch (error) {
            onboardingEmailStatus.className = "onboarding-email-status is-error";
            onboardingEmailStatus.textContent = error instanceof TypeError
                ? "Unable to reach the server. Check your connection and try again."
                : error.message;
        } finally {
            onboardingEmailButton.disabled = false;
            onboardingEmailButton.classList.remove("is-loading");
            onboardingEmailButton.removeAttribute("aria-busy");
        }
    });
}
