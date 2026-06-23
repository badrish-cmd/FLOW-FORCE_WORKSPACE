document.addEventListener(
    "DOMContentLoaded",
    function () {

        const form =
            document.getElementById(
                "forgotPasswordForm"
            );

        if (!form) {
            return;
        }

        form.addEventListener(
            "submit",
            function () {

                const button =
                    document.getElementById(
                        "sendOtpBtn"
                    );

                button.disabled = true;

                button.innerHTML =
                    '<i class="fas fa-spinner fa-spin me-2"></i>Sending OTP...';

            }
        );

    }
);