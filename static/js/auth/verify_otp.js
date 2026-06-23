const otpInputs =
    document.querySelectorAll(".otp-input");

otpInputs.forEach((input, index) => {

    input.addEventListener("input", () => {

        if (
            input.value &&
            index < otpInputs.length - 1
        ) {
            otpInputs[index + 1].focus();
        }

    });

});

document
.getElementById("otpForm")
.addEventListener(
    "submit",
    function () {

        let otp = "";

        otpInputs.forEach(input => {
            otp += input.value;
        });

        document
        .getElementById("otpHidden")
        .value = otp;

    }
);