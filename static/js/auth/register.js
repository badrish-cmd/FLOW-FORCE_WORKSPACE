document.addEventListener("DOMContentLoaded", () => {

    const password =
        document.getElementById("password");

    const confirmPassword =
        document.getElementById("confirm_password");

    const email =
        document.getElementById("email");

    const strength =
        document.getElementById("passwordStrength");

    password.addEventListener(
        "input",
        function(){

            const value = this.value;

            if(value.length < 6){

                strength.innerHTML =
                    "Weak Password";

                strength.className =
                    "password-strength strength-weak";

            }
            else if(value.length < 10){

                strength.innerHTML =
                    "Good Password";

                strength.className =
                    "password-strength strength-good";

            }
            else{

                strength.innerHTML =
                    "Strong Password";

                strength.className =
                    "password-strength strength-strong";
            }
        }
    );

    document
        .getElementById("registerForm")
        .addEventListener(
            "submit",
            function(e){

                const emailVal = email.value.toLowerCase().trim();
                if(
                    !emailVal.endsWith("@flow-force.com") &&
                    !emailVal.endsWith("@flowforceengineering.com")
                ){
                    e.preventDefault();

                    alert(
                        "Only @flow-force.com or @flowforceengineering.com emails are allowed."
                    );

                    return;
                }

                if(
                    password.value !==
                    confirmPassword.value
                ){
                    e.preventDefault();

                    alert(
                        "Passwords do not match."
                    );
                }
            }
        );
});