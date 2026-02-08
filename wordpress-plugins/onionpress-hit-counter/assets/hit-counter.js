/**
 * OnionPress Hit Counter - Animated Odometer
 */
(function($) {
    'use strict';

    class HitCounter {
        constructor(element) {
            this.$element = $(element);
            this.autoIncrement = this.$element.data('auto-increment') === 'true' || this.$element.data('auto-increment') === true;
            this.currentCount = parseInt(this.$element.data('current-count')) || 0;
            this.hasIncremented = false;

            // Increment on page load if enabled
            if (this.autoIncrement && !this.hasIncremented) {
                this.increment();
            }
        }

        /**
         * Increment the counter via AJAX
         */
        increment() {
            if (this.hasIncremented) {
                return; // Only increment once per page load
            }

            $.ajax({
                url: onionpressCounter.ajax_url,
                type: 'POST',
                data: {
                    action: 'increment_counter',
                    nonce: onionpressCounter.nonce
                },
                success: (response) => {
                    if (response.success) {
                        const newCount = response.data.count;
                        const formattedCount = response.data.formatted;

                        this.animateToNewValue(formattedCount);
                        this.currentCount = newCount;
                        this.hasIncremented = true;
                    }
                },
                error: (xhr, status, error) => {
                    console.error('Hit counter increment failed:', error);
                }
            });
        }

        /**
         * Animate counter to new value
         */
        animateToNewValue(newFormattedValue) {
            const $digits = this.$element.find('.counter-digit');
            const newDigits = newFormattedValue.split('');

            $digits.each(function(index) {
                const $digit = $(this);
                const currentDigit = $digit.data('digit');
                const newDigit = newDigits[index];

                if (currentDigit !== newDigit) {
                    // Animate the digit change
                    const $inner = $digit.find('.digit-inner');

                    // Add spinning class
                    $digit.addClass('spinning');

                    // After animation, update the digit
                    setTimeout(() => {
                        $inner.text(newDigit);
                        $digit.data('digit', newDigit);
                        $digit.removeClass('spinning');
                    }, 300);
                }
            });
        }
    }

    /**
     * Initialize all hit counters on the page
     */
    $(document).ready(function() {
        $('.onionpress-hit-counter').each(function() {
            new HitCounter(this);
        });
    });

})(jQuery);
