document.addEventListener(
    "DOMContentLoaded",
    function () {

        const newPassword =
            document.getElementById(
                "newPassword"
            );

        const confirmPassword =
            document.getElementById(
                "confirmPassword"
            );

        const toggleNewPassword =
            document.getElementById(
                "toggleNewPassword"
            );

        const toggleConfirmPassword =
            document.getElementById(
                "toggleConfirmPassword"
            );

        const strengthBar =
            document.getElementById(
                "strengthBar"
            );

        const strengthText =
            document.getElementById(
                "strengthText"
            );

        const form =
            document.getElementById(
                "resetPasswordForm"
            );

        // SHOW / HIDE NEW PASSWORD

        toggleNewPassword.addEventListener(
            "click",
            function () {

                if (
                    newPassword.type ===
                    "password"
                ) {

                    newPassword.type =
                        "text";

                    this.innerHTML =
                        '<i class="fas fa-eye-slash"></i>';

                } else {

                    newPassword.type =
                        "password";

                    this.innerHTML =
                        '<i class="fas fa-eye"></i>';

                }

            }
        );

        // SHOW / HIDE CONFIRM PASSWORD

        toggleConfirmPassword.addEventListener(
            "click",
            function () {

                if (
                    confirmPassword.type ===
                    "password"
                ) {

                    confirmPassword.type =
                        "text";

                    this.innerHTML =
                        '<i class="fas fa-eye-slash"></i>';

                } else {

                    confirmPassword.type =
                        "password";

                    this.innerHTML =
                        '<i class="fas fa-eye"></i>';

                }

            }
        );

        // PASSWORD STRENGTH

        newPassword.addEventListener(
            "input",
            function () {

                let score = 0;

                const password =
                    this.value;

                if (
                    password.length >= 8
                ) score++;

                if (
                    /[A-Z]/.test(password)
                ) score++;

                if (
                    /[0-9]/.test(password)
                ) score++;

                if (
                    /[^A-Za-z0-9]/.test(
                        password
                    )
                ) score++;

                switch (score) {

                    case 1:

                        strengthBar.style.width =
                            "25%";

                        strengthBar.className =
                            "progress-bar bg-danger";

                        strengthText.innerHTML =
                            "Weak";

                        break;

                    case 2:

                        strengthBar.style.width =
                            "50%";

                        strengthBar.className =
                            "progress-bar bg-warning";

                        strengthText.innerHTML =
                            "Medium";

                        break;

                    case 3:

                        strengthBar.style.width =
                            "75%";

                        strengthBar.className =
                            "progress-bar bg-info";

                        strengthText.innerHTML =
                            "Good";

                        break;

                    case 4:

                        strengthBar.style.width =
                            "100%";

                        strengthBar.className =
                            "progress-bar bg-success";

                        strengthText.innerHTML =
                            "Strong";

                        break;

                    default:

                        strengthBar.style.width =
                            "0%";

                        strengthText.innerHTML =
                            "Password Strength";

                }

            }
        );

        // FORM VALIDATION

        form.addEventListener(
            "submit",
            function (e) {

                if (
                    newPassword.value !==
                    confirmPassword.value
                ) {

                    e.preventDefault();

                    alert(
                        "Passwords do not match."
                    );

                    return;
                }

                const button =
                    document.querySelector(
                        'button[type="submit"]'
                    );

                button.disabled =
                    true;

                button.innerHTML =
                    "Changing Password...";

            }
        );

    }
);